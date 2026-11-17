"""Connectors for official sanctions lists.

The element names follow the publicly documented XML exports as known at the time of writing:
  * OFAC SDN / Consolidated "legacy" XML (sdnList / sdnEntry)
  * UN Security Council Consolidated List XML (CONSOLIDATED_LIST / INDIVIDUAL / ENTITY)
  * EU Financial Sanctions Files XML (export / sanctionEntity / nameAlias)
Source formats change. Each connector fails closed (ListFormatError) if it cannot find what it
expects, and ``tests/integration/test_live_lists.py`` (opt-in) checks them against live files.
See Agent.md section 9: verify current URLs and formats before production use.
UK lists are intentionally not shipped until the current source format is verified.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from lxml import etree

from swift_screening.lists.connectors.base import (
    ListConnector,
    ListFormatError,
    child,
    children,
    descendants,
    iter_records,
    local,
    text,
)
from swift_screening.lists.model import EntryIdentifier, EntryType, WatchlistEntry


def _join(*parts: str | None) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())


def _parse_date(value: str, *formats: str) -> datetime | None:
    for fmt in formats:
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


ID_TYPE_MAP = {
    "swift/bic": "BIC",
    "bic": "BIC",
    "swift": "BIC",
    "lei": "LEI",
    "legal entity number": "LEI",
    "imo": "IMO",
    "vessel registration identification": "IMO",
    "passport": "PASSPORT",
    "national id no.": "NATIONAL_ID",
    "national id": "NATIONAL_ID",
    "id card": "NATIONAL_ID",
    "id": "NATIONAL_ID",
    "mmsi": "MMSI",
    "digital currency address": "CRYPTO_ADDRESS",
}

# OFAC's idList also carries descriptive attributes that are NOT identifiers. Indexing them as
# identifiers would make e.g. an ID value "Male" or "2005" exact-match thousands of entries.
# (Found by validating against the live SDN file: ~26k of ~68k idList items are attributes.)
ATTRIBUTE_ID_TYPE_WORDS = (
    "gender",
    "risk",
    "date",
    "type",
    "year",
    "model",
    "flag",
    "website",
    "email",
    "phone",
    "information",
    "prohibited",
    "operator",
    "nationality",
    "tonnage",
    "length",
    "owner",
    "listing",
    "status",
    "sanctions",
    "title",
)


def _is_attribute(raw_type: str | None) -> bool:
    key = (raw_type or "").strip().lower()
    return any(word in key for word in ATTRIBUTE_ID_TYPE_WORDS)


def _id_type(raw: str | None) -> str:
    key = (raw or "").strip().lower()
    for k, v in ID_TYPE_MAP.items():
        if key == k or key.startswith(k):
            return v
    return "OTHER"


class OfacSdnConnector(ListConnector):
    list_id = "OFAC_SDN"
    default_url = (
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"
    )

    def parse(self, data: bytes) -> tuple[list[WatchlistEntry], datetime | None]:
        published = None
        m = re.search(rb"<(?:\w+:)?Publish_Date>([^<]+)<", data[:20000])
        if m:
            published = _parse_date(m.group(1).decode(), "%m/%d/%Y", "%Y-%m-%d")
        entries: list[WatchlistEntry] = []
        for el in iter_records(data, "sdnEntry"):
            uid = text(el, "uid")
            if not uid:
                raise ListFormatError("OFAC sdnEntry without uid")
            sdn_type = (text(el, "sdnType") or "").lower()
            entry_type: EntryType = {
                "individual": "individual",
                "entity": "organisation",
                "vessel": "vessel",
                "aircraft": "aircraft",
            }.get(sdn_type, "unknown")  # type: ignore[assignment]
            name = _join(text(el, "firstName"), text(el, "lastName"))
            if not name:
                raise ListFormatError(f"OFAC entry {uid} has no name")
            aliases: list[str] = []
            weak: list[str] = []
            for aka in descendants(el, "aka"):
                alias = _join(text(aka, "firstName"), text(aka, "lastName"))
                if alias:
                    (weak if (text(aka, "category") or "").lower() == "weak" else aliases).append(
                        alias
                    )
            idents = []
            attributes: dict[str, list[str]] = {}
            for id_el in descendants(el, "id"):
                number = text(id_el, "idNumber")
                raw_type = text(id_el, "idType")
                if number and _is_attribute(raw_type):
                    attributes.setdefault((raw_type or "").rstrip(":- "), []).append(number)
                elif number:
                    idents.append(
                        EntryIdentifier(
                            type=_id_type(text(id_el, "idType")),
                            value=number,
                            country=text(id_el, "idCountry"),
                        )
                    )
            vessel = child(el, "vesselInfo")
            if vessel is not None and (imo := text(vessel, "callSign")):
                idents.append(EntryIdentifier(type="CALL_SIGN", value=imo))
            entries.append(
                WatchlistEntry(
                    list_id=self.list_id,
                    entry_id=uid,
                    entry_type=entry_type,
                    name=name,
                    aliases=aliases,
                    weak_aliases=weak,
                    identifiers=idents,
                    dates_of_birth=[
                        d
                        for x in descendants(el, "dateOfBirthItem")
                        if (d := text(x, "dateOfBirth"))
                    ],
                    nationalities=[
                        n for x in descendants(el, "nationality") if (n := text(x, "country"))
                    ],
                    countries=sorted(
                        {c for x in descendants(el, "address") if (c := text(x, "country"))}
                    ),
                    programs=[p for x in descendants(el, "program") if (p := text(x))],
                    remarks=text(el, "remarks"),
                    attributes=attributes,
                )
            )
        return entries, published


class UnConsolidatedConnector(ListConnector):
    list_id = "UN_CONSOLIDATED"
    default_url = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"

    def parse(self, data: bytes) -> tuple[list[WatchlistEntry], datetime | None]:
        published = None
        m = re.search(rb'dateGenerated="([^"]+)"', data[:4000])
        if m:
            published = _parse_date(m.group(1).decode()[:19], "%Y-%m-%dT%H:%M:%S")
        entries: list[WatchlistEntry] = []
        for el in iter_records(data, "INDIVIDUAL"):
            entries.append(self._record(el, "individual"))
        for el in iter_records(data, "ENTITY"):
            entries.append(self._record(el, "organisation"))
        return entries, published

    def _record(self, el: etree._Element, entry_type: EntryType) -> WatchlistEntry:
        dataid = text(el, "DATAID") or text(el, "REFERENCE_NUMBER")
        if not dataid:
            raise ListFormatError("UN record without DATAID")
        name = _join(
            text(el, "FIRST_NAME"),
            text(el, "SECOND_NAME"),
            text(el, "THIRD_NAME"),
            text(el, "FOURTH_NAME"),
        )
        if not name:
            raise ListFormatError(f"UN record {dataid} has no name")
        aliases: list[str] = []
        weak: list[str] = []
        alias_tag = "INDIVIDUAL_ALIAS" if entry_type == "individual" else "ENTITY_ALIAS"
        for a in children(el, alias_tag):
            alias = text(a, "ALIAS_NAME")
            if alias:
                quality = (text(a, "QUALITY") or "").lower()
                (weak if quality == "low" else aliases).append(alias)
        if orig := text(el, "NAME_ORIGINAL_SCRIPT"):
            aliases.append(orig)
        idents = [
            EntryIdentifier(
                type=_id_type(text(d, "TYPE_OF_DOCUMENT")),
                value=num,
                country=text(d, "ISSUING_COUNTRY"),
            )
            for d in children(el, "INDIVIDUAL_DOCUMENT")
            if (num := text(d, "NUMBER"))
        ]
        dobs = []
        for d in children(el, "INDIVIDUAL_DATE_OF_BIRTH"):
            if v := text(d, "DATE") or text(d, "YEAR"):
                dobs.append(v)
        nats = [
            v for n in children(el, "NATIONALITY") for x in children(n, "VALUE") if (v := text(x))
        ]
        addr_tag = "INDIVIDUAL_ADDRESS" if entry_type == "individual" else "ENTITY_ADDRESS"
        countries = sorted({c for a in children(el, addr_tag) if (c := text(a, "COUNTRY"))})
        return WatchlistEntry(
            list_id=self.list_id,
            entry_id=dataid,
            entry_type=entry_type,
            name=name,
            aliases=aliases,
            weak_aliases=weak,
            identifiers=idents,
            dates_of_birth=dobs,
            nationalities=nats,
            countries=countries,
            programs=[p for p in [text(el, "UN_LIST_TYPE")] if p],
            remarks=text(el, "COMMENTS1"),
        )


class EuFsfConnector(ListConnector):
    list_id = "EU_FSF"
    default_url = "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw"

    def parse(self, data: bytes) -> tuple[list[WatchlistEntry], datetime | None]:
        published = None
        m = re.search(rb'generationDate="([^"]+)"', data[:4000])
        if m:
            published = _parse_date(m.group(1).decode()[:19], "%Y-%m-%dT%H:%M:%S")
        entries: list[WatchlistEntry] = []
        for el in iter_records(data, "sanctionEntity"):
            logical_id = el.get("logicalId") or el.get("euReferenceNumber")
            if not logical_id:
                raise ListFormatError("EU sanctionEntity without logicalId")
            subject = child(el, "subjectType")
            code = (subject.get("code") if subject is not None else "") or ""
            entry_type: EntryType = {"person": "individual", "enterprise": "organisation"}.get(
                code, "unknown"
            )  # type: ignore[assignment]
            names = []
            for na in children(el, "nameAlias"):
                whole = na.get("wholeName") or _join(
                    na.get("firstName"), na.get("middleName"), na.get("lastName")
                )
                if whole:
                    names.append(whole.strip())
            if not names:
                raise ListFormatError(f"EU entry {logical_id} has no nameAlias")
            idents = [
                EntryIdentifier(
                    type=_id_type(i.get("identificationTypeCode")),
                    value=num,
                    country=i.get("countryIso2Code"),
                )
                for i in children(el, "identification")
                if (num := i.get("number"))
            ]
            entries.append(
                WatchlistEntry(
                    list_id=self.list_id,
                    entry_id=logical_id,
                    entry_type=entry_type,
                    name=names[0],
                    aliases=list(dict.fromkeys(names[1:])),
                    identifiers=idents,
                    dates_of_birth=[
                        d
                        for b in children(el, "birthdate")
                        if (d := b.get("birthdate") or b.get("year"))
                    ],
                    nationalities=[
                        c for x in children(el, "citizenship") if (c := x.get("countryIso2Code"))
                    ],
                    countries=sorted(
                        {c for x in children(el, "address") if (c := x.get("countryIso2Code"))}
                        - {"00"}
                    ),
                    programs=[p for r in children(el, "regulation") if (p := r.get("programme"))],
                    remarks=text(child(el, "remark")),
                )
            )
        if not entries and b"sanctionEntity" not in data:
            raise ListFormatError("EU file contains no sanctionEntity elements")
        return entries, published


__all__ = ["EuFsfConnector", "OfacSdnConnector", "UnConsolidatedConnector", "local"]
