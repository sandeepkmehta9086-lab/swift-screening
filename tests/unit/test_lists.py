"""Watchlist management scenarios F.1 - F.6, connector formats, freshness and shrink guard."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from swift_screening.lists.connectors.base import ListFetchError, ListFormatError, fetch_source
from swift_screening.lists.connectors.internal import CsvListConnector, JsonListConnector
from swift_screening.lists.connectors.official import (
    EuFsfConnector,
    OfacSdnConnector,
    UnConsolidatedConnector,
)
from swift_screening.lists.model import ListType
from swift_screening.lists.registry import connector_for, refresh_list
from swift_screening.lists.store import ListShrinkError, ListStore

# Small, fictional samples in the shape of the official exports.
OFAC_XML = b"""<?xml version="1.0" standalone="yes"?>
<sdnList xmlns="https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML">
  <publshInformation><Publish_Date>11/13/2026</Publish_Date><Record_Count>2</Record_Count></publshInformation>
  <sdnEntry>
    <uid>90001</uid><firstName>Kornel</firstName><lastName>VASHTARIN</lastName>
    <sdnType>Individual</sdnType>
    <programList><program>FICTION-1</program></programList>
    <idList><id><uid>1</uid><idType>Passport</idType><idNumber>X998877</idNumber><idCountry>Fictonia</idCountry></id></idList>
    <akaList>
      <aka><uid>2</uid><type>a.k.a.</type><category>strong</category><lastName>VASHTARYN</lastName><firstName>Kornell</firstName></aka>
      <aka><uid>3</uid><type>a.k.a.</type><category>weak</category><lastName>The Bear</lastName></aka>
    </akaList>
    <dateOfBirthList><dateOfBirthItem><uid>4</uid><dateOfBirth>02 Mar 1966</dateOfBirth></dateOfBirthItem></dateOfBirthList>
  </sdnEntry>
  <sdnEntry>
    <uid>90002</uid><lastName>FICTIVE MARITIME CO</lastName><sdnType>Entity</sdnType>
    <programList><program>FICTION-2</program></programList>
    <idList><id><uid>5</uid><idType>SWIFT/BIC</idType><idNumber>FCTVKPPY</idNumber></id></idList>
    <addressList><address><uid>6</uid><city>Nampo</city><country>North Korea</country></address></addressList>
  </sdnEntry>
</sdnList>"""

UN_XML = """<?xml version="1.0" encoding="UTF-8"?>
<CONSOLIDATED_LIST dateGenerated="2026-11-12T08:00:00.000-05:00">
 <INDIVIDUALS>
  <INDIVIDUAL>
   <DATAID>7000001</DATAID><FIRST_NAME>ZORAN</FIRST_NAME><SECOND_NAME>FICTOVIC</SECOND_NAME>
   <UN_LIST_TYPE>Fiction</UN_LIST_TYPE><REFERENCE_NUMBER>FIi.001</REFERENCE_NUMBER>
   <NAME_ORIGINAL_SCRIPT>Зоран Фиктович</NAME_ORIGINAL_SCRIPT>
   <NATIONALITY><VALUE>Fictonia</VALUE></NATIONALITY>
   <INDIVIDUAL_ALIAS><QUALITY>Good</QUALITY><ALIAS_NAME>Zoran Fiktovich</ALIAS_NAME></INDIVIDUAL_ALIAS>
   <INDIVIDUAL_ALIAS><QUALITY>Low</QUALITY><ALIAS_NAME>Z.F.</ALIAS_NAME></INDIVIDUAL_ALIAS>
   <INDIVIDUAL_DATE_OF_BIRTH><TYPE_OF_DATE>EXACT</TYPE_OF_DATE><DATE>1970-05-05</DATE></INDIVIDUAL_DATE_OF_BIRTH>
   <INDIVIDUAL_DOCUMENT><TYPE_OF_DOCUMENT>Passport</TYPE_OF_DOCUMENT><NUMBER>FP-1</NUMBER></INDIVIDUAL_DOCUMENT>
  </INDIVIDUAL>
 </INDIVIDUALS>
 <ENTITIES>
  <ENTITY><DATAID>7000002</DATAID><FIRST_NAME>FICTION FRONT ENTERPRISES</FIRST_NAME>
   <ENTITY_ALIAS><QUALITY>Good</QUALITY><ALIAS_NAME>FFE</ALIAS_NAME></ENTITY_ALIAS>
   <ENTITY_ADDRESS><COUNTRY>Fictonia</COUNTRY></ENTITY_ADDRESS></ENTITY>
 </ENTITIES>
</CONSOLIDATED_LIST>""".encode()

EU_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<export xmlns="http://eu.europa.ec/fpi/fsd/export" generationDate="2026-11-12T10:00:00.000+01:00" globalFileId="1">
 <sanctionEntity designationDetails="" unitedNationId="" euReferenceNumber="EU.1.1" logicalId="501">
  <regulation programme="FICT"/>
  <subjectType code="person"/>
  <nameAlias firstName="Ilse" lastName="Fabelhaft" wholeName="Ilse Fabelhaft"/>
  <nameAlias wholeName="Ilse Fabelhaftova"/>
  <citizenship countryIso2Code="DE"/>
  <birthdate birthdate="1975-07-07" year="1975"/>
  <identification number="FAB-77" identificationTypeCode="passport" countryIso2Code="DE"/>
 </sanctionEntity>
 <sanctionEntity euReferenceNumber="EU.2.2" logicalId="502">
  <subjectType code="enterprise"/>
  <nameAlias wholeName="Fabel Industries GmbH"/>
  <address countryIso2Code="00"/>
 </sanctionEntity>
</export>"""


def write(tmp: Path, name: str, data: bytes) -> str:
    p = tmp / name
    p.write_bytes(data)
    return str(p)


# ------------------------------------------------------------------ connectors
@pytest.mark.scenario("F.1")
def test_ofac_connector() -> None:
    snap = OfacSdnConnector().snapshot(OFAC_XML, "test")
    assert snap.published_at == datetime(2026, 11, 13, tzinfo=UTC)
    e1, e2 = snap.entries
    assert e1.entry_id == "90001" and e1.name == "Kornel VASHTARIN"
    assert e1.entry_type == "individual"
    assert e1.aliases == ["Kornell VASHTARYN"] and e1.weak_aliases == ["The Bear"]
    assert e1.identifiers[0].type == "PASSPORT" and e1.identifiers[0].value == "X998877"
    assert e1.dates_of_birth == ["02 Mar 1966"] and e1.programs == ["FICTION-1"]
    assert e2.entry_type == "organisation" and e2.identifiers[0].type == "BIC"
    assert e2.countries == ["North Korea"]
    assert snap.version.startswith("2026-11-13T00:00:00Z#")


@pytest.mark.scenario("F.1")
def test_un_connector() -> None:
    snap = UnConsolidatedConnector().snapshot(UN_XML, "test")
    ind, ent = snap.entries
    assert ind.name == "ZORAN FICTOVIC" and "Зоран Фиктович" in ind.aliases
    assert "Zoran Fiktovich" in ind.aliases and ind.weak_aliases == ["Z.F."]
    assert ind.dates_of_birth == ["1970-05-05"] and ind.identifiers[0].type == "PASSPORT"
    assert ent.entry_type == "organisation" and ent.name == "FICTION FRONT ENTERPRISES"
    assert snap.published_at is not None and snap.published_at.year == 2026


@pytest.mark.scenario("F.1")
def test_eu_connector() -> None:
    snap = EuFsfConnector().snapshot(EU_XML, "test")
    person, org = snap.entries
    assert person.entry_id == "501" and person.name == "Ilse Fabelhaft"
    assert person.aliases == ["Ilse Fabelhaftova"] and person.nationalities == ["DE"]
    assert person.identifiers[0].type == "PASSPORT"
    assert org.entry_type == "organisation" and org.countries == []


def test_csv_and_json_connectors(tmp_path: Path) -> None:
    csv_data = b"entry_id,name,aliases,bic\nA1,Test Person,T. Person;Tess P,ZYXBIRTH\n"
    snap = CsvListConnector("X").snapshot(csv_data, "t")
    assert snap.entries[0].aliases == ["T. Person", "Tess P"]
    assert snap.entries[0].identifiers[0].type == "BIC"
    js = json.dumps({"entries": [{"entry_id": "1", "name": "A"}]}).encode()
    assert JsonListConnector("Y").snapshot(js, "t").entries[0].list_id == "Y"


@pytest.mark.scenario("F.4")
@pytest.mark.parametrize(
    "connector,data",
    [
        (OfacSdnConnector(), OFAC_XML[: len(OFAC_XML) // 2]),  # truncated download
        (OfacSdnConnector(), b"<html>Service unavailable</html>"),
        (UnConsolidatedConnector(), b"<CONSOLIDATED_LIST></CONSOLIDATED_LIST>"),
        (EuFsfConnector(), b"<export></export>"),
        (JsonListConnector("J"), b"{not json"),
        (JsonListConnector("J"), b'{"entries": [{"name": "missing id"}]}'),
        (CsvListConnector("C"), b"wrong,header\n1,2\n"),
        (OfacSdnConnector(), b'<!DOCTYPE x [<!ENTITY e "x">]><sdnList/>'),
    ],
)
def test_malformed_sources_raise_format_error(connector, data: bytes) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ListFormatError):
        connector.snapshot(data, "test")


def test_duplicate_ids_rejected() -> None:
    data = json.dumps(
        {"entries": [{"entry_id": "1", "name": "A"}, {"entry_id": "1", "name": "B"}]}
    ).encode()
    with pytest.raises(ListFormatError):
        JsonListConnector("J").snapshot(data, "t")


# ------------------------------------------------------------------ store / versioning
def _json_list(
    entries: list[tuple[str, str]], published: str = "2026-11-01T00:00:00+00:00"
) -> bytes:
    return json.dumps(
        {
            "published_at": published,
            "list_type": "sanctions",
            "entries": [{"entry_id": i, "name": n} for i, n in entries],
        }
    ).encode()


@pytest.mark.scenario("F.2")
def test_update_produces_diff_and_keeps_history(tmp_path: Path) -> None:
    store = ListStore(tmp_path / "s")
    c = JsonListConnector("L", ListType.SANCTIONS)
    v1 = write(
        tmp_path,
        "v1.json",
        _json_list([("1", "Alpha One"), ("2", "Beta Two"), ("3", "Gamma Three")]),
    )
    v2 = write(
        tmp_path,
        "v2.json",
        _json_list(
            [("1", "Alpha One"), ("2", "Beta Twoo"), ("4", "Delta Four")],
            "2026-11-02T00:00:00+00:00",
        ),
    )
    d1 = refresh_list(store, c, v1)
    assert d1.from_version is None and d1.added == ["1", "2", "3"]
    d2 = refresh_list(store, c, v2)
    assert d2.added == ["4"] and d2.removed == ["3"] and d2.changed == ["2"]
    assert len(store.versions("L")) == 2
    old = store.load("L", d1.to_version)
    assert {e.entry_id for e in old.entries} == {"1", "2", "3"}
    assert store.current("L").version == d2.to_version  # type: ignore[union-attr]


def test_same_content_same_version(tmp_path: Path) -> None:
    store = ListStore(tmp_path / "s")
    c = JsonListConnector("L", ListType.SANCTIONS)
    src = write(tmp_path, "v.json", _json_list([("1", "Alpha One")]))
    a = refresh_list(store, c, src)
    b = refresh_list(store, c, src)
    assert a.to_version == b.to_version and not (b.added or b.removed or b.changed)


@pytest.mark.scenario("F.3")
def test_unreachable_source_keeps_last_good_and_records_failure(tmp_path: Path) -> None:
    store = ListStore(tmp_path / "s")
    c = JsonListConnector("L", ListType.SANCTIONS)
    good = refresh_list(store, c, write(tmp_path, "v.json", _json_list([("1", "Alpha One")])))
    with pytest.raises(ListFetchError):
        refresh_list(store, c, str(tmp_path / "missing.json"))
    st = store.state("L")
    assert st.current_version == good.to_version
    assert st.consecutive_failures == 1 and "not found" in (st.last_error or "")


@pytest.mark.scenario("F.3")
def test_http_fetch_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, content=b"ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert (
        fetch_source("https://lists.example/x", retries=3, backoff_seconds=0, client=client)
        == b"ok"
    )
    assert calls["n"] == 3


def test_http_fetch_gives_up_after_retries() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(ListFetchError):
        fetch_source("https://lists.example/x", retries=2, backoff_seconds=0, client=client)


@pytest.mark.scenario("F.4")
def test_malformed_update_keeps_last_good(tmp_path: Path) -> None:
    store = ListStore(tmp_path / "s")
    c = JsonListConnector("L", ListType.SANCTIONS)
    good = refresh_list(store, c, write(tmp_path, "v.json", _json_list([("1", "Alpha One")])))
    with pytest.raises(ListFormatError):
        refresh_list(store, c, write(tmp_path, "bad.json", b"{broken"))
    assert store.state("L").current_version == good.to_version
    assert store.state("L").last_error


@pytest.mark.scenario("L.7")
def test_shrink_guard_rejects_suspicious_snapshot(tmp_path: Path) -> None:
    store = ListStore(tmp_path / "s")
    c = JsonListConnector("L", ListType.SANCTIONS)
    full = [(str(i), f"Name Number{i}") for i in range(10)]
    good = refresh_list(store, c, write(tmp_path, "full.json", _json_list(full)))
    small = write(tmp_path, "small.json", _json_list(full[:4]))
    with pytest.raises(ListShrinkError):
        refresh_list(store, c, small)
    assert store.state("L").current_version == good.to_version
    forced = refresh_list(store, c, small, force=True)
    assert store.state("L").current_version == forced.to_version


@pytest.mark.scenario("F.5")
def test_internal_list_upload(tmp_path: Path) -> None:
    store = ListStore(tmp_path / "s")
    src = write(tmp_path, "i.csv", b"entry_id,name,entry_type\nI1,Internal Person,individual\n")
    refresh_list(store, connector_for("csv", "TENANT_X_INTERNAL", ListType.INTERNAL), src)
    snap = store.current("TENANT_X_INTERNAL")
    assert snap is not None and snap.list_type == ListType.INTERNAL


@pytest.mark.scenario("L.6")
def test_freshness_uses_last_successful_check(tmp_path: Path) -> None:
    store = ListStore(tmp_path / "s")
    c = JsonListConnector("L", ListType.SANCTIONS)
    snap = c.snapshot(_json_list([("1", "Alpha One")], "2020-01-01T00:00:00+00:00"), "t")
    t0 = datetime(2026, 11, 15, 12, tzinfo=UTC)
    store.save(snap, now=t0)
    # Old publication date but just checked: fresh.
    assert not store.status("L", 36, now=t0 + timedelta(hours=1)).stale
    st = store.status("L", 36, now=t0 + timedelta(hours=37))
    assert st.stale and st.age_hours == 37.0
    # Never-loaded list counts as stale.
    assert store.status("NEVER", 36).stale


OFAC_WITH_ATTRIBUTES = b"""<?xml version="1.0"?>
<sdnList><publshInformation><Publish_Date>11/13/2026</Publish_Date></publshInformation>
<sdnEntry><uid>91</uid><firstName>Test</firstName><lastName>FICTIVEPERSON</lastName>
 <sdnType>Individual</sdnType><programList><program>F</program></programList>
 <idList>
  <id><uid>1</uid><idType>Gender</idType><idNumber>Male</idNumber></id>
  <id><uid>2</uid><idType>Secondary sanctions risk:</idType><idNumber>Section 1(b)</idNumber></id>
  <id><uid>3</uid><idType>Vessel Year of Build</idType><idNumber>2005</idNumber></id>
  <id><uid>4</uid><idType>Website</idType><idNumber>www.example.invalid</idNumber></id>
  <id><uid>5</uid><idType>Tax ID No.</idType><idNumber>77012345</idNumber></id>
  <id><uid>6</uid><idType>MMSI</idType><idNumber>273456789</idNumber></id>
 </idList></sdnEntry></sdnList>"""


def test_ofac_attributes_are_not_identifiers() -> None:
    """Regression from validating against the live SDN file (~26k attribute idList items)."""
    e = OfacSdnConnector().snapshot(OFAC_WITH_ATTRIBUTES, "t").entries[0]
    assert {i.type for i in e.identifiers} == {"OTHER", "MMSI"}
    assert {i.value for i in e.identifiers} == {"77012345", "273456789"}
    assert e.attributes["Gender"] == ["Male"]
    assert e.attributes["Vessel Year of Build"] == ["2005"]


def test_index_ignores_implausible_identifiers() -> None:
    from swift_screening.lists.model import (
        EntryIdentifier,
        ListSnapshot,
        WatchlistEntry,
        content_hash,
    )
    from swift_screening.matching.index import WatchlistIndex

    entries = [
        WatchlistEntry(
            list_id="T",
            entry_id="1",
            name="X",
            identifiers=[
                EntryIdentifier(type="OTHER", value="Male"),
                EntryIdentifier(type="OTHER", value="2005"),
                EntryIdentifier(type="OTHER", value="REG-1234567"),
                EntryIdentifier(type="BIC", value="NOTBIC"),
            ],
        )
    ]
    idx = WatchlistIndex(
        [
            ListSnapshot(
                list_id="T",
                list_type=ListType.SANCTIONS,
                source="t",
                content_hash=content_hash(entries),
                entries=entries,
            )
        ]
    )
    assert idx.lookup_identifier("OTHER", "MALE") == []
    assert idx.lookup_identifier("OTHER", "2005") == []
    assert idx.lookup_identifier("OTHER", "reg 1234567") == [0]
    assert idx.lookup_identifier("BIC", "NOTBIC") == []


def test_unchanged_source_without_publish_date_keeps_version(tmp_path: Path) -> None:
    """Found by the docker compose restart test: a CSV list (no publication date) got a new
    version on every reload, breaking idempotency and piling up snapshots."""
    store = ListStore(tmp_path / "s")
    src = write(tmp_path, "i.csv", b"entry_id,name\nI1,Internal Person\n")
    t0 = datetime(2026, 11, 15, 10, tzinfo=UTC)
    c = CsvListConnector("INT")
    store.save(c.snapshot(Path(src).read_bytes(), src), now=t0)
    v1 = store.state("INT").current_version
    store.save(c.snapshot(Path(src).read_bytes(), src), now=t0 + timedelta(hours=5))
    assert store.state("INT").current_version == v1
    assert len(store.versions("INT")) == 1
    assert store.state("INT").last_success_at == t0 + timedelta(hours=5)  # freshness renewed
    Path(src).write_bytes(b"entry_id,name\nI1,Internal Person\nI2,Second Person\n")
    store.save(c.snapshot(Path(src).read_bytes(), src), now=t0 + timedelta(hours=6))
    assert store.state("INT").current_version != v1
