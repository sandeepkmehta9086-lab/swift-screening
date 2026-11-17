"""ISO 20022 MX parser for pacs.008 and pacs.009 (incl. COV) -> CPM.

Security: DTDs/entities are rejected outright (XXE, billion laughs), network access is disabled,
and size / depth limits are enforced before mapping.

Validation: if an XSD for the exact message version exists in ``schemas/iso20022/`` it is used
(``<msg>.xsd``, e.g. ``pacs.008.001.08.xsd``); otherwise a built-in structural check of the
elements the screening depends on is applied. Either failure is a fail-closed parse error.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections.abc import Iterable, Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from lxml import etree

from swift_screening.cpm.identifiers import (
    bic_country,
    iban_country,
    is_valid_bic,
    is_valid_iban,
    normalize_bic,
)
from swift_screening.cpm.models import (
    CanonicalPayment,
    FreeText,
    Identifier,
    IdentifierType,
    ParseWarning,
    Party,
    PartyRole,
    PostalAddress,
    Provenance,
)
from swift_screening.parsing.errors import ParseError

MAX_XML_BYTES = 5_000_000
MAX_DEPTH = 40
MAX_ELEMENTS = 200_000
XSD_DIR = Path(__file__).resolve().parents[4] / "schemas" / "iso20022"

NS_RE = re.compile(r"urn:iso:std:iso:20022:tech:xsd:((pacs|pain|camt|head)\.\d{3}\.\d{3}\.\d{2})")
SUPPORTED_FAMILIES = {"pacs.008", "pacs.009"}

_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    dtd_validation=False,
    huge_tree=False,
    remove_comments=True,
    remove_pis=True,
)


def _local(tag: object) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _child(el: etree._Element | None, *names: str) -> etree._Element | None:
    cur = el
    for name in names:
        if cur is None:
            return None
        nxt = None
        for c in cur:
            if _local(c.tag) == name:
                nxt = c
                break
        cur = nxt
    return cur


def _children(el: etree._Element | None, name: str) -> list[etree._Element]:
    if el is None:
        return []
    return [c for c in el if _local(c.tag) == name]


def _text(el: etree._Element | None, *names: str) -> str | None:
    target = _child(el, *names) if names else el
    if target is None or target.text is None:
        return None
    value = target.text.strip()
    return value or None


def _depth_and_count(root: etree._Element) -> tuple[int, int]:
    max_depth, count = 0, 0
    stack: list[tuple[etree._Element, int]] = [(root, 1)]
    while stack:
        el, d = stack.pop()
        count += 1
        max_depth = max(max_depth, d)
        if count > MAX_ELEMENTS or max_depth > MAX_DEPTH:
            break
        stack.extend((c, d + 1) for c in el if isinstance(c.tag, str))
    return max_depth, count


class _Ctx:
    def __init__(self, message_type: str) -> None:
        self.message_type = message_type
        self.warnings: list[ParseWarning] = []

    def warn(
        self, code: str, message: str, path: str | None = None, *, error: bool = False
    ) -> None:
        self.warnings.append(
            ParseWarning(
                code=code, message=message, path=path, severity="error" if error else "warning"
            )
        )

    def prov(self, path: str) -> Provenance:
        return Provenance(source_format="MX", message_type=self.message_type, path=f"MX:{path}")


def _address(el: etree._Element | None) -> PostalAddress | None:
    if el is None:
        return None
    return PostalAddress(
        lines=[t for a in _children(el, "AdrLine") if (t := _text(a))],
        street=_text(el, "StrtNm"),
        building=_text(el, "BldgNb"),
        post_code=_text(el, "PstCd"),
        town=_text(el, "TwnNm"),
        country_sub_division=_text(el, "CtrySubDvsn"),
        country=_text(el, "Ctry"),
    )


def _address_lines(addr: PostalAddress | None) -> list[str]:
    if addr is None:
        return []
    structured = [
        x
        for x in (addr.street, addr.building, addr.post_code, addr.town, addr.country_sub_division)
        if x
    ]
    return [*addr.lines, *([" ".join(structured)] if structured else [])]


def _party(el: etree._Element, role: PartyRole, path: str, ctx: _Ctx, tx: int | None) -> Party:
    """PartyIdentification (Dbtr, Cdtr, UltmtDbtr, UltmtCdtr, InitgPty)."""
    party = Party(role=role, transaction_index=tx, provenance=[ctx.prov(path)])
    party.name = _text(el, "Nm")
    party.address = _address(_child(el, "PstlAdr"))
    party.country = party.address.country if party.address else None
    party.country_of_residence = _text(el, "CtryOfRes")
    party.name_lines = [*([party.name] if party.name else []), *_address_lines(party.address)]
    org = _child(el, "Id", "OrgId")
    prv = _child(el, "Id", "PrvtId")
    if org is not None:
        party.party_type = "organisation"
        if bic := _text(org, "AnyBIC"):
            party.identifiers.append(_bic_identifier(bic, f"{path}/Id/OrgId/AnyBIC", ctx))
        if lei := _text(org, "LEI"):
            party.identifiers.append(
                Identifier(
                    type=IdentifierType.LEI, value=lei, provenance=ctx.prov(f"{path}/Id/OrgId/LEI")
                )
            )
        for o in _children(org, "Othr"):
            if v := _text(o, "Id"):
                party.identifiers.append(
                    Identifier(
                        type=IdentifierType.OTHER,
                        value=v,
                        provenance=ctx.prov(f"{path}/Id/OrgId/Othr"),
                    )
                )
    if prv is not None:
        party.party_type = "individual"
        dob = _child(prv, "DtAndPlcOfBirth")
        if dob is not None:
            if d := _text(dob, "BirthDt"):
                party.identifiers.append(Identifier(type=IdentifierType.DATE_OF_BIRTH, value=d))
            city, ctry = _text(dob, "CityOfBirth"), _text(dob, "CtryOfBirth")
            if city or ctry:
                party.identifiers.append(
                    Identifier(type=IdentifierType.PLACE_OF_BIRTH, value=city or "", country=ctry)
                )
        for o in _children(prv, "Othr"):
            if v := _text(o, "Id"):
                code = (_text(o, "SchmeNm", "Cd") or "").upper()
                id_type = {
                    "CCPT": IdentifierType.PASSPORT,
                    "NIDN": IdentifierType.NATIONAL_ID,
                    "CUST": IdentifierType.CUSTOMER_ID,
                }.get(code, IdentifierType.OTHER)
                party.identifiers.append(
                    Identifier(type=id_type, value=v, provenance=ctx.prov(f"{path}/Id/PrvtId/Othr"))
                )
    if not party.name:
        ctx.warn("MISSING_NAME", f"{path} has no Nm", f"MX:{path}")
    return party


def _bic_identifier(bic: str, path: str, ctx: _Ctx) -> Identifier:
    norm = normalize_bic(bic)
    valid = is_valid_bic(norm)
    if not valid:
        ctx.warn("INVALID_BIC", f"{path} BIC {norm!r} invalid", f"MX:{path}", error=True)
    return Identifier(
        type=IdentifierType.BIC,
        value=norm,
        country=bic_country(norm),
        valid=valid,
        provenance=ctx.prov(path),
    )


def _agent(el: etree._Element, role: PartyRole, path: str, ctx: _Ctx, tx: int | None) -> Party:
    """BranchAndFinancialInstitutionIdentification (…Agt, FI Dbtr/Cdtr in pacs.009)."""
    party = Party(
        role=role, party_type="institution", transaction_index=tx, provenance=[ctx.prov(path)]
    )
    fin = _child(el, "FinInstnId")
    if fin is None:
        ctx.warn("MISSING_FININSTNID", f"{path} has no FinInstnId", f"MX:{path}", error=True)
        return party
    if bic := _text(fin, "BICFI"):
        ident = _bic_identifier(bic, f"{path}/FinInstnId/BICFI", ctx)
        party.identifiers.append(ident)
        party.country = ident.country
    if lei := _text(fin, "LEI"):
        party.identifiers.append(
            Identifier(
                type=IdentifierType.LEI, value=lei, provenance=ctx.prov(f"{path}/FinInstnId/LEI")
            )
        )
    if mmb := _text(fin, "ClrSysMmbId", "MmbId"):
        party.identifiers.append(Identifier(type=IdentifierType.CLEARING_CODE, value=mmb))
    party.name = _text(fin, "Nm")
    party.address = _address(_child(fin, "PstlAdr"))
    if party.address and party.address.country:
        party.country = party.country or party.address.country
    party.name_lines = [*([party.name] if party.name else []), *_address_lines(party.address)]
    return party


def _account(el: etree._Element | None, party: Party | None, path: str, ctx: _Ctx) -> None:
    if el is None or party is None:
        return
    if iban := _text(el, "Id", "IBAN"):
        valid = is_valid_iban(iban)
        if not valid:
            ctx.warn("IBAN_CHECKSUM", f"{path} IBAN fails validation", f"MX:{path}")
        party.identifiers.append(
            Identifier(
                type=IdentifierType.IBAN,
                value=iban.upper(),
                country=iban_country(iban),
                valid=valid,
                provenance=ctx.prov(f"{path}/Id/IBAN"),
            )
        )
    elif other := _text(el, "Id", "Othr", "Id"):
        party.identifiers.append(
            Identifier(
                type=IdentifierType.ACCOUNT, value=other, provenance=ctx.prov(f"{path}/Id/Othr/Id")
            )
        )


AGENT_ROLES: list[tuple[str, PartyRole]] = [
    ("InstgAgt", PartyRole.INSTRUCTING_AGENT),
    ("InstdAgt", PartyRole.INSTRUCTED_AGENT),
    ("PrvsInstgAgt1", PartyRole.PREVIOUS_INSTRUCTING_AGENT),
    ("PrvsInstgAgt2", PartyRole.PREVIOUS_INSTRUCTING_AGENT),
    ("PrvsInstgAgt3", PartyRole.PREVIOUS_INSTRUCTING_AGENT),
    ("IntrmyAgt1", PartyRole.INTERMEDIARY),
    ("IntrmyAgt2", PartyRole.INTERMEDIARY),
    ("IntrmyAgt3", PartyRole.INTERMEDIARY),
    ("DbtrAgt", PartyRole.DEBTOR_AGENT),
    ("CdtrAgt", PartyRole.CREDITOR_AGENT),
]
CUSTOMER_ROLES: list[tuple[str, PartyRole, str | None]] = [
    ("UltmtDbtr", PartyRole.ULTIMATE_DEBTOR, None),
    ("InitgPty", PartyRole.ULTIMATE_DEBTOR, None),
    ("Dbtr", PartyRole.ORDERING_CUSTOMER, "DbtrAcct"),
    ("Cdtr", PartyRole.BENEFICIARY, "CdtrAcct"),
    ("UltmtCdtr", PartyRole.ULTIMATE_CREDITOR, None),
]


def _map_transaction(
    tx_el: etree._Element,
    idx: int,
    base: str,
    ctx: _Ctx,
    cpm: CanonicalPayment,
    *,
    fi_parties: bool,
    seq: str | None = None,
) -> None:
    for name, role in AGENT_ROLES:
        if (el := _child(tx_el, name)) is not None:
            p = _agent(el, role, f"{base}/{name}", ctx, idx)
            p.sequence = seq
            cpm.parties.append(p)
    for name, role, acct in CUSTOMER_ROLES:
        el = _child(tx_el, name)
        if el is None:
            continue
        is_fi = fi_parties and name in {"Dbtr", "Cdtr"}
        if is_fi:
            fi_role = (
                PartyRole.ORDERING_INSTITUTION
                if name == "Dbtr"
                else (PartyRole.BENEFICIARY_INSTITUTION)
            )
            p = _agent(el, fi_role, f"{base}/{name}", ctx, idx)
        else:
            p = _party(el, role, f"{base}/{name}", ctx, idx)
        p.sequence = seq
        if acct:
            _account(_child(tx_el, acct), p, f"{base}/{acct}", ctx)
        cpm.parties.append(p)

    rmt = _child(tx_el, "RmtInf")
    for i, u in enumerate(_children(rmt, "Ustrd")):
        if t := _text(u):
            cpm.remittance_info.append(
                FreeText(
                    kind="remittance",
                    text=t,
                    transaction_index=idx,
                    provenance=ctx.prov(f"{base}/RmtInf/Ustrd[{i}]"),
                )
            )
    for i, s in enumerate(_children(rmt, "Strd")):
        texts = [str(t).strip() for t in s.itertext() if str(t).strip()]
        if texts:
            cpm.remittance_info.append(
                FreeText(
                    kind="remittance",
                    text=" ".join(texts),
                    transaction_index=idx,
                    provenance=ctx.prov(f"{base}/RmtInf/Strd[{i}]"),
                )
            )
    for tag in ("InstrForNxtAgt", "InstrForCdtrAgt"):
        for i, ins in enumerate(_children(tx_el, tag)):
            if t := _text(ins, "InstrInf"):
                cpm.instructions.append(
                    FreeText(
                        kind="instruction",
                        code=_text(ins, "Cd"),
                        text=t,
                        transaction_index=idx,
                        provenance=ctx.prov(f"{base}/{tag}[{i}]"),
                    )
                )
    for i, rr in enumerate(_children(tx_el, "RgltryRptg")):
        texts = [str(t).strip() for t in rr.itertext() if str(t).strip()]
        if texts:
            cpm.regulatory_reporting.append(
                FreeText(
                    kind="regulatory",
                    text=" ".join(texts),
                    transaction_index=idx,
                    provenance=ctx.prov(f"{base}/RgltryRptg[{i}]"),
                )
            )
    for code in (_text(tx_el, "Purp", "Cd"), _text(tx_el, "Purp", "Prtry")):
        if code:
            cpm.purpose_codes.append(code)


def _iter_elements(root: etree._Element, name: str) -> Iterator[etree._Element]:
    for el in root.iter():
        if _local(el.tag) == name:
            yield el


def _structural_check(doc: etree._Element, family: str, ctx: _Ctx) -> None:
    body = doc[0] if len(doc) else None
    expected_body = {"pacs.008": "FIToFICstmrCdtTrf", "pacs.009": "FICdtTrf"}[family]
    if body is None or _local(body.tag) != expected_body:
        ctx.warn(
            "XSD_STRUCTURE", f"Document must contain {expected_body}", "MX:Document", error=True
        )
        return
    if _text(body, "GrpHdr", "MsgId") is None:
        ctx.warn("XSD_STRUCTURE", "GrpHdr/MsgId missing", "MX:GrpHdr/MsgId", error=True)
    txs = _children(body, "CdtTrfTxInf")
    if not txs:
        ctx.warn("XSD_STRUCTURE", "no CdtTrfTxInf", "MX:CdtTrfTxInf", error=True)
    for i, tx in enumerate(txs):
        base = f"CdtTrfTxInf[{i}]"
        required = ["PmtId", "IntrBkSttlmAmt", "Dbtr", "Cdtr"]
        if family == "pacs.008":
            required += ["DbtrAgt", "CdtrAgt"]
        for req in required:
            if _child(tx, req) is None:
                ctx.warn("XSD_STRUCTURE", f"{base}/{req} missing", f"MX:{base}/{req}", error=True)
        amt = _child(tx, "IntrBkSttlmAmt")
        if amt is not None:
            if not re.fullmatch(r"\d{1,18}(\.\d{1,5})?", (amt.text or "").strip()):
                ctx.warn(
                    "XSD_STRUCTURE",
                    f"{base}/IntrBkSttlmAmt not a decimal",
                    f"MX:{base}/IntrBkSttlmAmt",
                    error=True,
                )
            if not re.fullmatch(r"[A-Z]{3}", amt.get("Ccy", "")):
                ctx.warn(
                    "XSD_STRUCTURE",
                    f"{base}/IntrBkSttlmAmt@Ccy invalid",
                    f"MX:{base}/IntrBkSttlmAmt",
                    error=True,
                )


def _xsd_validate(doc: etree._Element, version: str, ctx: _Ctx) -> bool:
    xsd_path = XSD_DIR / f"{version}.xsd"
    if not xsd_path.is_file():
        return False
    schema = etree.XMLSchema(etree.parse(str(xsd_path), _PARSER))
    if not schema.validate(etree.ElementTree(doc)):
        errors = list(cast(Iterable[Any], schema.error_log))  # stubs lack __iter__
        for err in errors[:20]:
            ctx.warn("XSD_INVALID", err.message, f"MX:line {err.line}", error=True)
    return True


def parse_mx(raw: bytes | str) -> CanonicalPayment:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not data.strip():
        raise ParseError("empty message")
    if len(data) > MAX_XML_BYTES:
        raise ParseError(f"XML exceeds {MAX_XML_BYTES} bytes")
    head = data[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data.upper():
        raise ParseError("DTD / entity declarations are not allowed", path="MX:prolog")
    try:
        root = etree.fromstring(data, _PARSER)
    except etree.XMLSyntaxError as exc:
        raise ParseError(f"XML not well-formed: {exc}") from exc
    depth, count = _depth_and_count(root)
    if depth > MAX_DEPTH or count > MAX_ELEMENTS:
        raise ParseError("XML exceeds depth / element limits")

    doc = root if _local(root.tag) == "Document" else next(_iter_elements(root, "Document"), None)
    if doc is None:
        raise ParseError("no ISO 20022 Document element found", path="MX:Document")
    m = NS_RE.search(etree.QName(doc.tag).namespace or "")
    if not m:
        raise ParseError(
            "Document namespace is not an ISO 20022 message namespace", path="MX:Document"
        )
    version = m.group(1)
    family = version[:8]
    ctx = _Ctx(version)

    cpm = CanonicalPayment(
        source_format="MX", message_type=version, raw_hash=hashlib.sha256(data).hexdigest()
    )

    # Business Application Header (head.001), if present anywhere in the envelope.
    app_hdr = next(_iter_elements(root, "AppHdr"), None)
    if app_hdr is not None:
        for name, role in (("Fr", PartyRole.SENDER), ("To", PartyRole.RECEIVER)):
            bic = _text(app_hdr, name, "FIId", "FinInstnId", "BICFI")
            if bic:
                ident = _bic_identifier(bic, f"AppHdr/{name}", ctx)
                cpm.parties.append(
                    Party(
                        role=role,
                        party_type="institution",
                        country=ident.country,
                        identifiers=[ident],
                        provenance=[ctx.prov(f"AppHdr/{name}")],
                    )
                )
        if (dup := _text(app_hdr, "PssblDplct")) and dup.lower() == "true":
            cpm.possible_duplicate = True

    if family not in SUPPORTED_FAMILIES:
        ctx.warn(
            "UNSUPPORTED_MESSAGE_TYPE",
            f"{version} not fully supported; screened generically",
            "MX:Document",
            error=True,
        )
        _generic_text(doc, cpm, ctx)
        cpm.parse_warnings = ctx.warnings
        return cpm

    if not _xsd_validate(doc, version, ctx):
        _structural_check(doc, family, ctx)

    body = doc[0] if len(doc) else doc
    cpm.message_id = _text(body, "GrpHdr", "MsgId")
    txs = _children(body, "CdtTrfTxInf")
    cpm.transaction_count = max(1, len(txs))
    for i, tx in enumerate(txs):
        base = f"CdtTrfTxInf[{i}]"
        if i == 0:
            cpm.uetr = _text(tx, "PmtId", "UETR")
            amt = _child(tx, "IntrBkSttlmAmt")
            if amt is not None and amt.text:
                with contextlib.suppress(InvalidOperation):
                    cpm.amount = Decimal(amt.text.strip())
                cpm.currency = amt.get("Ccy")
        _map_transaction(tx, i, base, ctx, cpm, fi_parties=family == "pacs.009")
        cov = _child(tx, "UndrlygCstmrCdtTrf")
        if cov is not None:
            cpm.message_type = f"{version}.COV" if family == "pacs.009" else version
            _map_transaction(
                cov, i, f"{base}/UndrlygCstmrCdtTrf", ctx, cpm, fi_parties=False, seq="B"
            )
            for req in ("Dbtr", "Cdtr"):
                if _child(cov, req) is None:
                    ctx.warn(
                        "XSD_STRUCTURE",
                        f"COV underlying {req} missing",
                        f"MX:{base}/UndrlygCstmrCdtTrf/{req}",
                        error=True,
                    )
    cpm.parse_warnings = ctx.warnings
    return cpm


def _generic_text(doc: etree._Element, cpm: CanonicalPayment, ctx: _Ctx) -> None:
    """Fallback for unsupported types: every name-like / text element is screened."""
    for el in doc.iter():
        name = _local(el.tag)
        if not el.text or not el.text.strip() or len(el) > 0:
            continue
        value = el.text.strip()
        if name in {"BICFI", "AnyBIC"}:
            cpm.parties.append(
                Party(
                    role=PartyRole.MENTIONED,
                    party_type="institution",
                    identifiers=[_bic_identifier(value, name, ctx)],
                )
            )
        elif name in {"Nm", "AdrLine", "Ustrd", "InstrInf", "TwnNm", "AddtlInf", "Inf"}:
            cpm.unmapped_fields.append(
                FreeText(kind="unmapped", text=value, provenance=ctx.prov(name))
            )
        elif name == "Ctry":
            cpm.parties.append(Party(role=PartyRole.MENTIONED, country=value))
