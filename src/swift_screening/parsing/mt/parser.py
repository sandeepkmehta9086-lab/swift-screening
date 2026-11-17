"""SWIFT MT (FIN) parser: blocks {1}-{5}, block-4 field grammar, party options -> CPM.

Design rules:
* Never silently drop content. Unknown fields go to ``unmapped_fields`` and are screened as text.
* Structural problems are recorded as ``ParseWarning(severity="error")`` instead of aborting,
  so whatever could be extracted is still screened and the decision is forced to >= REVIEW.
* Only truly unusable input (empty, oversized, no fields at all) raises ``ParseError``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from swift_screening.cpm.identifiers import (
    bic_country,
    iban_country,
    is_valid_bic,
    is_valid_iban,
    looks_like_iban,
    normalize_bic,
    normalize_iban,
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

MAX_MESSAGE_CHARS = 100_000
MAX_FIELDS = 500

# SWIFT "X" character set (CR/LF handled as line breaks).
X_CHARSET_RE = re.compile(r"^[A-Za-z0-9/\-?:().,'+ ]*$")
FIELD_START_RE = re.compile(r"^:(\d{2}[A-Z]?):(.*)$")
F_LINE_RE = re.compile(r"^([1-8])/(.*)$")
F_PARTY_ID_RE = re.compile(r"^(ARNU|CCPT|CUST|DRLC|EMPL|NIDN|SOSE|TXID)/([A-Z]{2})?/?(.*)$")
CODEWORD_RE = re.compile(r"^/([A-Z0-9]{1,8})/(.*)$")
FIELD20_RE = re.compile(r"^(?!/)(?!.*//)(?!.*/$).{1,16}$")

PARTY_TAG_ROLES: dict[str, PartyRole] = {
    "50": PartyRole.ORDERING_CUSTOMER,
    "51": PartyRole.ORDERING_INSTITUTION,
    "52": PartyRole.ORDERING_INSTITUTION,
    "53": PartyRole.SENDERS_CORRESPONDENT,
    "54": PartyRole.RECEIVERS_CORRESPONDENT,
    "55": PartyRole.THIRD_REIMBURSEMENT,
    "56": PartyRole.INTERMEDIARY,
    "57": PartyRole.ACCOUNT_WITH_INSTITUTION,
    "58": PartyRole.BENEFICIARY_INSTITUTION,
    "59": PartyRole.BENEFICIARY,
}
CUSTOMER_TAGS = {"50", "59"}

ALLOWED_OPTIONS: dict[str, set[str]] = {
    "50": {"A", "F", "K"},
    "51": {"A"},
    "52": {"A", "D"},
    "53": {"A", "B", "D"},
    "54": {"A", "B", "D"},
    "55": {"A", "B", "D"},
    "56": {"A", "C", "D"},
    "57": {"A", "B", "C", "D"},
    "58": {"A", "D"},
    "59": {"", "A", "F"},
}

# (max lines, max chars per line) for free-format fields
LINE_LIMITS: dict[str, tuple[int, int]] = {
    "70": (4, 35),
    "72": (6, 35),
    "77B": (3, 35),
}
PARTY_TEXT_LIMIT = (4, 35)  # name & address lines for K, D and no-letter options

MANDATORY: dict[str, list[str]] = {
    "MT103": ["20", "23B", "32A", "50", "59", "71A"],
    "MT202": ["20", "21", "32A", "58"],
    "MT202COV": ["20", "21", "32A", "58", "B:50", "B:59"],
}
SUPPORTED_TYPES = set(MANDATORY)

FREE_TEXT_TAGS: dict[str, str] = {
    "70": "remittance",
    "72": "sender_to_receiver",
    "77B": "regulatory",
    "77T": "remittance",
    "23E": "instruction",
    "26T": "instruction",
}
KNOWN_NON_PARTY_TAGS = {"21", "13C", "23B", "32A", "33B", "36", "71A", "71F", "71G"}


@dataclass
class _Field:
    tag: str  # full tag e.g. "50K"
    lines: list[str]
    index: int
    sequence: str = "A"

    @property
    def number(self) -> str:
        return self.tag[:2]

    @property
    def option(self) -> str:
        return self.tag[2:]

    @property
    def value(self) -> str:
        return "\n".join(self.lines)


@dataclass
class _Blocks:
    b1: str | None = None
    b2: str | None = None
    b3: str | None = None
    b4: str | None = None
    b5: str | None = None
    extra: list[str] = field(default_factory=list)


def _split_blocks(raw: str) -> _Blocks:
    """Split ``{1:...}{2:...}{3:{..}}{4:\\n...\\n-}{5:{..}}`` into blocks (brace-aware)."""
    blocks = _Blocks()
    i, n = 0, len(raw)
    while i < n:
        if raw[i].isspace():
            i += 1
            continue
        if raw[i] != "{":
            raise ParseError(f"unexpected character {raw[i]!r} outside blocks at offset {i}")
        colon = raw.find(":", i)
        if colon == -1:
            raise ParseError("block without identifier")
        block_id = raw[i + 1 : colon]
        if block_id == "4":
            end = raw.find("\n-}", colon)
            if end == -1:
                raise ParseError("block 4 is not terminated by '-}'", path="MT:block4")
            blocks.b4 = raw[colon + 1 : end + 1]
            i = end + 3
            continue
        depth, j = 0, i
        while j < n:
            if raw[j] == "{":
                depth += 1
            elif raw[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            raise ParseError(f"block {block_id} is not closed", path=f"MT:block{block_id}")
        content = raw[colon + 1 : j]
        if block_id in {"1", "2", "3", "5"}:
            setattr(blocks, f"b{block_id}", content)
        else:
            blocks.extra.append(block_id)
        i = j + 1
    return blocks


def _parse_fields(b4: str) -> list[_Field]:
    fields: list[_Field] = []
    for raw_line in b4.split("\n"):
        line = raw_line.rstrip("\r")
        m = FIELD_START_RE.match(line)
        if m:
            fields.append(_Field(tag=m.group(1), lines=[m.group(2)], index=len(fields)))
            if len(fields) > MAX_FIELDS:
                raise ParseError(f"more than {MAX_FIELDS} fields", path="MT:block4")
        elif fields:
            fields[-1].lines.append(line)
        elif line.strip():
            raise ParseError("block 4 content before first field tag", path="MT:block4")
    for f in fields:  # trailing blank lines are formatting, not content
        while len(f.lines) > 1 and f.lines[-1] == "":
            f.lines.pop()
    return fields


def _sub_block_values(content: str | None) -> dict[str, str]:
    if not content:
        return {}
    return {m.group(1): m.group(2) for m in re.finditer(r"\{([A-Z0-9]{3}):([^}]*)\}", content)}


class _Builder:
    def __init__(self) -> None:
        self.warnings: list[ParseWarning] = []
        self.message_type = "MT"

    def warn(
        self, code: str, message: str, path: str | None = None, *, error: bool = False
    ) -> None:
        self.warnings.append(
            ParseWarning(
                code=code, message=message, path=path, severity="error" if error else "warning"
            )
        )

    def prov(self, path: str, line: int | None = None) -> Provenance:
        return Provenance(source_format="MT", message_type=self.message_type, path=path, line=line)


def _fpath(f: _Field) -> str:
    seq = "B/" if f.sequence == "B" else ""
    return f"MT:{seq}{f.tag}"


def _account_identifier(account: str, b: _Builder, path: str) -> Identifier:
    acct = account.strip()
    if acct.startswith(("/C/", "/D/")):  # debit/credit marks in 53a/57a
        acct = acct[2:]
    acct = acct.lstrip("/")
    if looks_like_iban(acct):
        iban = normalize_iban(acct)
        valid = is_valid_iban(iban)
        if not valid:
            b.warn("IBAN_CHECKSUM", f"IBAN {iban[:4]}... fails validation", path)
        return Identifier(
            type=IdentifierType.IBAN,
            value=iban,
            country=iban_country(iban),
            valid=valid,
            provenance=b.prov(path, 0),
        )
    return Identifier(type=IdentifierType.ACCOUNT, value=acct, provenance=b.prov(path, 0))


def _check_text_lines(f: _Field, lines: list[str], limit: tuple[int, int], b: _Builder) -> None:
    max_lines, max_chars = limit
    if len(lines) > max_lines:
        b.warn("TOO_MANY_LINES", f"{f.tag} has {len(lines)} lines (max {max_lines})", _fpath(f))
    if any(len(line) > max_chars for line in lines):
        b.warn("LINE_TOO_LONG", f"{f.tag} line exceeds {max_chars} chars", _fpath(f))


def _parse_f_option(f: _Field, lines: list[str], party: Party, b: _Builder) -> None:
    path = _fpath(f)
    name_parts: list[str] = []
    addr_lines: list[str] = []
    address = PostalAddress()
    offset = len(f.lines) - len(lines)
    for idx, line in enumerate(lines, start=offset):
        m = F_LINE_RE.match(line)
        if not m:
            b.warn("F_OPTION_LINE", f"{f.tag} line not in 'n/text' format", path, error=True)
            name_parts.append(line)  # still screen it
            continue
        num, text = m.group(1), m.group(2)
        if num == "1":
            name_parts.append(text)
        elif num == "2":
            addr_lines.append(text)
        elif num == "3":
            cc, _, town = text.partition("/")
            if re.fullmatch(r"[A-Z]{2}", cc):
                address.country = cc
                address.town = town or None
            else:
                b.warn("F_OPTION_COUNTRY", f"{f.tag} 3/ line lacks ISO country", path)
            addr_lines.append(text)
        elif num == "4" and f.number == "50":
            try:
                dob = date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
                party.identifiers.append(
                    Identifier(
                        type=IdentifierType.DATE_OF_BIRTH,
                        value=dob.isoformat(),
                        provenance=b.prov(path, idx),
                    )
                )
                party.party_type = "individual"
            except (ValueError, IndexError):
                b.warn("F_OPTION_DOB", f"{f.tag} 4/ date of birth invalid", path)
        elif num == "5" and f.number == "50":
            cc, _, place = text.partition("/")
            party.identifiers.append(
                Identifier(
                    type=IdentifierType.PLACE_OF_BIRTH,
                    value=place or text,
                    country=cc if re.fullmatch(r"[A-Z]{2}", cc) else None,
                    provenance=b.prov(path, idx),
                )
            )
            party.party_type = "individual"
        elif num in {"6", "7", "8"} and f.number == "50":
            parts = text.split("/")
            id_type = IdentifierType.NATIONAL_ID if num == "7" else IdentifierType.CUSTOMER_ID
            party.identifiers.append(
                Identifier(
                    type=id_type,
                    value=parts[-1],
                    country=parts[0] if len(parts) > 1 else None,
                    provenance=b.prov(path, idx),
                )
            )
        else:
            b.warn("F_OPTION_LINE", f"{f.tag} line number {num} not valid here", path)
            addr_lines.append(text)
    party.name = " ".join(p.strip() for p in name_parts if p.strip()) or None
    party.name_lines = name_parts + addr_lines
    address.lines = addr_lines
    party.address = address
    party.country = address.country
    if not party.name:
        b.warn("MISSING_NAME", f"{f.tag} has no 1/ name line", path, error=True)


def _parse_party(f: _Field, b: _Builder, role: PartyRole) -> Party:
    path = _fpath(f)
    opt = f.option
    lines = list(f.lines)
    party = Party(
        role=role,
        party_type="unknown" if f.number in CUSTOMER_TAGS else "institution",
        sequence=f.sequence if f.sequence == "B" else None,
        provenance=[b.prov(path)],
    )
    allowed = ALLOWED_OPTIONS.get(f.number)
    if allowed is not None and opt not in allowed:
        b.warn(
            "INVALID_OPTION", f"option {opt!r} not allowed for field {f.number}", path, error=True
        )

    # Optional party identifier / account line.
    first = lines[0] if lines else ""
    f_id = F_PARTY_ID_RE.match(first) if opt == "F" else None
    if f_id:
        code, country, value = f_id.group(1), f_id.group(2), f_id.group(3)
        id_type = {
            "CCPT": IdentifierType.PASSPORT,
            "NIDN": IdentifierType.NATIONAL_ID,
            "CUST": IdentifierType.CUSTOMER_ID,
        }.get(code, IdentifierType.OTHER)
        party.identifiers.append(
            Identifier(type=id_type, value=value, country=country, provenance=b.prov(path, 0))
        )
        lines = lines[1:]
    elif first.startswith("/"):
        party.identifiers.append(_account_identifier(first, b, path))
        lines = lines[1:]

    if opt == "A":
        bic_line = lines[0].strip() if lines else ""
        if not bic_line:
            b.warn("MISSING_BIC", f"{f.tag} has no BIC", path, error=True)
            return party
        bic = normalize_bic(bic_line)
        valid = is_valid_bic(bic)
        if not valid:
            b.warn("INVALID_BIC", f"{f.tag} BIC {bic!r} is not a valid BIC", path, error=True)
        party.identifiers.append(
            Identifier(
                type=IdentifierType.BIC,
                value=bic,
                country=bic_country(bic),
                valid=valid,
                provenance=b.prov(path, len(f.lines) - len(lines)),
            )
        )
        party.country = bic_country(bic)
        party.party_type = "institution"
        party.name_lines = lines  # anything after the BIC is still kept and screened
        return party

    if opt == "C":
        return party  # account only

    if opt == "B":
        party.name_lines = lines  # location
        if lines:
            party.address = PostalAddress(lines=lines)
        return party

    if opt == "F":
        _parse_f_option(f, lines, party, b)
        return party

    # K, D and no-letter (59): up to 4 lines of name and address.
    _check_text_lines(f, lines, PARTY_TEXT_LIMIT, b)
    party.name_lines = lines
    party.name = lines[0].strip() if lines and lines[0].strip() else None
    if len(lines) > 1:
        party.address = PostalAddress(lines=lines[1:])
    if party.name is None:
        b.warn("MISSING_NAME", f"{f.tag} has no name", path, error=True)
    return party


def _parse_codewords(f: _Field, b: _Builder, kind: str) -> list[FreeText]:
    """Field 72/77B: '/CODE/text' lines with '//' continuation lines."""
    segments: list[FreeText] = []
    path = _fpath(f)
    for idx, line in enumerate(f.lines):
        if line.startswith("//") and segments:
            seg = segments[-1]
            seg.text = f"{seg.text} {line[2:]}".strip()
            continue
        m = CODEWORD_RE.match(line)
        if m:
            segments.append(
                FreeText(
                    kind=kind,
                    code=m.group(1),
                    text=m.group(2).strip(),
                    provenance=b.prov(path, idx),
                )
            )
        else:
            segments.append(FreeText(kind=kind, text=line, provenance=b.prov(path, idx)))
    return [s for s in segments if s.text.strip() or s.code]


def _parse_32a(f: _Field, b: _Builder, cpm: CanonicalPayment) -> None:
    m = re.fullmatch(r"(\d{6})([A-Z]{3})(\d{1,15},\d{0,4})", f.value.strip())
    if not m:
        b.warn("INVALID_32A", "32A must be YYMMDD + currency + amount", _fpath(f), error=True)
        return
    yymmdd, ccy, amount = m.groups()
    try:
        cpm.value_date = date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
    except ValueError:
        b.warn("INVALID_32A", "32A date invalid", _fpath(f), error=True)
    cpm.currency = ccy
    try:
        cpm.amount = Decimal(amount.replace(",", "."))
    except InvalidOperation:
        b.warn("INVALID_32A", "32A amount invalid", _fpath(f), error=True)


def _lt_to_bic(lt: str) -> str:
    """Logical terminal address (BIC8 + terminal code + branch) -> BIC11."""
    return lt[:8] + lt[9:12]


def _institution(role: PartyRole, bic: str, path: str, b: _Builder) -> Party:
    return Party(
        role=role,
        party_type="institution",
        country=bic_country(bic),
        identifiers=[
            Identifier(type=IdentifierType.BIC, value=bic, country=bic_country(bic), valid=True)
        ],
        provenance=[b.prov(path)],
    )


def parse_mt(raw: str) -> CanonicalPayment:
    """Parse a raw MT message. Raises ParseError only when nothing usable can be extracted."""
    if not raw or not raw.strip():
        raise ParseError("empty message")
    if len(raw) > MAX_MESSAGE_CHARS:
        raise ParseError(f"message exceeds {MAX_MESSAGE_CHARS} characters")
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    b = _Builder()

    stripped = text.strip()
    has_blocks = stripped.startswith("{")
    if has_blocks:
        blocks = _split_blocks(stripped)
    else:
        b.warn(
            "MISSING_HEADER_BLOCKS",
            "no {1:}/{2:} blocks; treating input as block 4",
            "MT:block1",
            error=True,
        )
        blocks = _Blocks(b4=stripped.removesuffix("-"))
    if blocks.b4 is None:
        raise ParseError("block 4 (text block) missing", path="MT:block4")
    fields = _parse_fields(blocks.b4)
    if not fields:
        raise ParseError("block 4 contains no fields", path="MT:block4")

    b3 = _sub_block_values(blocks.b3)
    b5 = _sub_block_values(blocks.b5)

    # ---- Header blocks -----------------------------------------------------------------
    sender_bic: str | None = None
    receiver_bic: str | None = None
    mt_number: str | None = None
    if blocks.b1 is not None:
        if re.fullmatch(r"[FAL]\d{2}[A-Z0-9]{12}\d{10}", blocks.b1):
            sender_bic = _lt_to_bic(blocks.b1[3:15])
        else:
            b.warn("INVALID_BLOCK1", "basic header block malformed", "MT:block1", error=True)
    elif has_blocks:
        b.warn("MISSING_BLOCK1", "basic header block missing", "MT:block1", error=True)
    if blocks.b2 is not None:
        m_in = re.fullmatch(r"I(\d{3})([A-Z0-9]{12})([SUN](\d(\d{3})?)?)?", blocks.b2)
        m_out = re.fullmatch(
            r"O(\d{3})(\d{4})(\d{6})([A-Z0-9]{12})(\d{10})(\d{10})([SUN])?", blocks.b2
        )
        if m_in:
            mt_number = m_in.group(1)
            receiver_bic = _lt_to_bic(m_in.group(2))
        elif m_out:
            # Output message: block 1 holds the receiver, the MIR holds the sender.
            mt_number = m_out.group(1)
            receiver_bic, sender_bic = sender_bic, _lt_to_bic(m_out.group(4))
        else:
            b.warn("INVALID_BLOCK2", "application header block malformed", "MT:block2", error=True)
            m_any = re.match(r"[IO](\d{3})", blocks.b2)
            mt_number = m_any.group(1) if m_any else None
    elif has_blocks:
        b.warn("MISSING_BLOCK2", "application header block missing", "MT:block2", error=True)

    numbers = {f.number for f in fields}
    if mt_number is None:  # inferred, but the missing header is already an error (fail closed)
        if "58" in numbers:
            mt_number = "202"
        elif "59" in numbers or "50" in numbers:
            mt_number = "103"
    is_cov = mt_number == "202" and (b3.get("119") == "COV" or "50" in numbers)
    if is_cov:
        message_type = "MT202COV"
    else:
        message_type = f"MT{mt_number}" if mt_number else "MT"
    b.message_type = message_type
    if mt_number == "202" and "50" in numbers and b3.get("119") != "COV":
        b.warn("COV_WITHOUT_119", "MT202 with customer sequence treated as MT202COV", "MT:block3")
    if message_type not in SUPPORTED_TYPES:
        b.warn(
            "UNSUPPORTED_MESSAGE_TYPE",
            f"{message_type} is not fully supported; all content screened generically",
            "MT:block2",
            error=True,
        )

    cpm = CanonicalPayment(
        source_format="MT",
        message_type=message_type,
        uetr=b3.get("121"),
        raw_hash=hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest(),
        possible_duplicate="PDE" in b5 or "PDM" in b5,
    )
    if sender_bic and is_valid_bic(sender_bic):
        cpm.parties.append(_institution(PartyRole.SENDER, sender_bic, "MT:block1", b))
    if receiver_bic and is_valid_bic(receiver_bic):
        cpm.parties.append(_institution(PartyRole.RECEIVER, receiver_bic, "MT:block2", b))

    # ---- Sequence assignment (MT202COV sequence B starts at the first 50a) ---------------
    if is_cov:
        in_b = False
        for f in fields:
            in_b = in_b or f.number == "50"
            if in_b:
                f.sequence = "B"

    # ---- Fields --------------------------------------------------------------------------
    for f in fields:
        path = _fpath(f)
        if any(not X_CHARSET_RE.match(line) for line in f.lines):
            b.warn("CHARSET", f"{f.tag} contains characters outside the SWIFT X set", path)
        if f.number in PARTY_TAG_ROLES:
            cpm.parties.append(_parse_party(f, b, PARTY_TAG_ROLES[f.number]))
        elif f.tag in FREE_TEXT_TAGS:
            kind = FREE_TEXT_TAGS[f.tag]
            if f.tag in LINE_LIMITS:
                _check_text_lines(f, f.lines, LINE_LIMITS[f.tag], b)
            if f.tag in {"72", "77B"}:
                segs = _parse_codewords(f, b, kind)
            else:
                joined = " ".join(ln.strip() for ln in f.lines if ln.strip())
                segs = [FreeText(kind=kind, text=joined, provenance=b.prov(path))]
            if kind == "remittance":
                cpm.remittance_info.extend(segs)
            elif kind == "regulatory":
                cpm.regulatory_reporting.extend(segs)
            else:
                cpm.instructions.extend(segs)
        elif f.tag == "20":
            cpm.message_id = f.value.strip()
            if not FIELD20_RE.match(cpm.message_id):
                b.warn(
                    "INVALID_20", "field 20 must be 1-16x, no leading/trailing '/' or '//'", path
                )
        elif f.tag == "21":
            cpm.related_reference = f.value.strip()
        elif f.tag == "32A":
            _parse_32a(f, b, cpm)
        elif f.tag in KNOWN_NON_PARTY_TAGS:
            continue
        else:
            b.warn("UNKNOWN_FIELD", f"field {f.tag} not mapped; screened as free text", path)
            cpm.unmapped_fields.append(
                FreeText(kind="unmapped", text=" ".join(f.lines), provenance=b.prov(path))
            )

    # ---- Mandatory fields ----------------------------------------------------------------
    present = {(f.sequence, f.number) for f in fields} | {(f.sequence, f.tag) for f in fields}
    for req in MANDATORY.get(message_type, []):
        seq, _, tag = req.rpartition(":")
        seq = seq or "A"
        if (seq, tag) not in present:
            b.warn(
                "MISSING_MANDATORY_FIELD",
                f"mandatory field {req} missing",
                f"MT:{'B/' if seq == 'B' else ''}{tag}",
                error=True,
            )

    cpm.parse_warnings = b.warnings
    return cpm
