"""CPM -> screening subjects (deterministic extraction + free-text rules).

Each subject carries provenance (field path) so every hit can point back to the exact field.
Extraction is deliberately redundant: a name line is screened as a name, joined with the next
line (split names), and the whole party block is screened again as free text (names hidden in
address lines). Redundancy costs CPU, missing a hit costs far more.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from swift_screening.cpm.identifiers import BIC_IN_TEXT_RE, IMO_RE, is_valid_bic, is_valid_imo
from swift_screening.cpm.models import CanonicalPayment, FreeText, IdentifierType, Party, PartyRole

SubjectKind = Literal["name", "text", "identifier", "country"]

MT_LINE_LIMIT = 35

# Phrases that introduce a hidden / third party inside free text.
HIDDEN_PARTY_RE = re.compile(
    r"(?:\bon\s+behalf\s+of\b|\bo\.?b\.?o\.?\b|\bf\.?f\.?c\.?\b|\bfor\s+further\s+credit\s+(?:to|of)\b"
    r"|\bin\s+favou?r\s+of\b|\bi\.?f\.?o\.?\b|\bultimate\s+(?:beneficiary|debtor|creditor)\b"
    r"|\bbeneficial\s+owner\b|\bfor\s+account\s+of\b|\bf/a/o\b|\bpayment\s+(?:to|for)\b)"
    r"[\s:,-]*(?P<name>[^\n,;]{3,105})",
    re.IGNORECASE,
)
# Field 72 / RgltryRptg codewords whose payload names a party or institution.
PARTY_CODEWORDS = {
    "BNF",
    "ACC",
    "INS",
    "INT",
    "REC",
    "PHONBEN",
    "TELEBEN",
    "ORDERRES",
    "BENEFRES",
    "UDLC",
    "OBO",
    "FFC",
}
RESIDENCE_CODE_RE = re.compile(r"^/?(ORDERRES|BENEFRES)/([A-Z]{2})\b")


@dataclass(slots=True, frozen=True)
class Subject:
    kind: SubjectKind
    value: str
    path: str
    role: str | None = None
    id_type: str | None = None
    party_type: str = "unknown"
    truncated: bool = False
    origin: str = "structured"  # structured | free_text_rule | free_text
    transaction_index: int | None = None


def _party_subjects(p: Party) -> list[Subject]:
    out: list[Subject] = []
    path = p.provenance[0].path if p.provenance else f"party:{p.role}"

    def mk(
        kind: SubjectKind,
        value: str,
        at: str,
        *,
        id_type: str | None = None,
        truncated: bool = False,
        origin: str = "structured",
    ) -> Subject:
        return Subject(
            kind,
            value,
            at,
            role=p.role.value,
            id_type=id_type,
            party_type=p.party_type,
            truncated=truncated,
            origin=origin,
            transaction_index=p.transaction_index,
        )

    for ident in p.identifiers:
        if ident.type in {
            IdentifierType.BIC,
            IdentifierType.LEI,
            IdentifierType.PASSPORT,
            IdentifierType.NATIONAL_ID,
            IdentifierType.IMO,
            IdentifierType.OTHER,
            IdentifierType.CUSTOMER_ID,
        }:
            out.append(mk("identifier", ident.value, path, id_type=ident.type.value))
        if ident.country:
            out.append(mk("country", ident.country, f"{path}#{ident.type.value}"))
    for c in {p.country, p.country_of_residence, p.address.country if p.address else None}:
        if c:
            out.append(mk("country", c, path))

    lines = [ln.strip() for ln in p.name_lines if ln and ln.strip()]
    if p.role in {PartyRole.SENDER, PartyRole.RECEIVER} and not lines:
        return out
    names: list[str] = []
    if p.name:
        names.append(p.name)
    if lines:
        names.append(lines[0])
        if len(lines) > 1:
            names.append(f"{lines[0]} {lines[1]}")  # name split across lines
            if len(lines[0]) >= MT_LINE_LIMIT - 2:
                names.append(lines[0] + lines[1])  # split mid-word at the line limit
    # Option-A lines that *are* the party's BIC are screened as identifiers, not as names.
    bics = {i.value for i in p.identifiers if i.type == IdentifierType.BIC}
    seen: set[str] = {n for n in names if n.replace(" ", "").upper() in bics}
    for n in names:
        # NB: never skip "BIC-looking" lines - an 8/11-letter name ("KIMCHOLSONG") fits the
        # BIC pattern; screening a real BIC as a name as well is harmless.
        if n in seen:
            continue
        seen.add(n)
        truncated = len(n) >= MT_LINE_LIMIT - 1 and n == (lines[0] if lines else "")
        out.append(mk("name", n, path, truncated=truncated))
    # Every line again as free text: names hidden in address lines, towns, regions.
    for i, line in enumerate(lines):
        if line.replace(" ", "").upper() in bics:
            continue  # the party's own BIC line: already screened as an exact identifier
        out.append(mk("text", line, f"{path}/line{i}", origin="free_text"))
    if p.address:
        for part in (p.address.town, p.address.country_sub_division, p.address.street):
            if part:
                out.append(mk("text", part, f"{path}/address", origin="free_text"))
    return out


def _free_text_subjects(ft: FreeText) -> list[Subject]:
    path = ft.provenance.path + (f"/{ft.code}" if ft.code else "")
    if ft.provenance.line is not None:
        path += f"#line{ft.provenance.line}"
    text = ft.text
    out = [
        Subject(
            "text",
            text,
            path,
            role=PartyRole.MENTIONED.value,
            origin="free_text",
            transaction_index=ft.transaction_index,
        )
    ]
    if ft.code in PARTY_CODEWORDS and text:
        out.append(
            Subject(
                "name",
                text,
                path,
                role=PartyRole.MENTIONED.value,
                origin="free_text_rule",
                transaction_index=ft.transaction_index,
            )
        )
    for m in HIDDEN_PARTY_RE.finditer(text):
        name = m.group("name").strip(" .:/-")
        if name:
            out.append(
                Subject(
                    "name",
                    name,
                    path,
                    role=PartyRole.MENTIONED.value,
                    origin="free_text_rule",
                    transaction_index=ft.transaction_index,
                )
            )
    for m in BIC_IN_TEXT_RE.finditer(text.upper()):
        if is_valid_bic(m.group(0)):
            out.append(
                Subject(
                    "identifier",
                    m.group(0),
                    path,
                    id_type="BIC",
                    role=PartyRole.MENTIONED.value,
                    origin="free_text",
                )
            )
    for m in IMO_RE.finditer(text):
        if is_valid_imo(m.group(1)):
            out.append(
                Subject(
                    "identifier",
                    m.group(1),
                    path,
                    id_type="IMO",
                    role=PartyRole.MENTIONED.value,
                    origin="free_text",
                )
            )
    full = f"/{ft.code}/{text}" if ft.code else text
    if (rm := RESIDENCE_CODE_RE.match(full)) is not None:
        out.append(Subject("country", rm.group(2), path, role=PartyRole.MENTIONED.value))
    return out


def extract_subjects(cpm: CanonicalPayment) -> list[Subject]:
    subjects: list[Subject] = []
    for p in cpm.parties:
        subjects.extend(_party_subjects(p))
    for ft in cpm.all_free_text():
        subjects.extend(_free_text_subjects(ft))
    # de-duplicate identical subjects (same kind/value/path)
    unique: dict[tuple[str, str, str], Subject] = {}
    for s in subjects:
        unique.setdefault((s.kind, s.value, s.path), s)
    return list(unique.values())
