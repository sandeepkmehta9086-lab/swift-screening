"""Additional MX mapping coverage: identifiers, private/org IDs, XSD path, agents, generic types."""

from __future__ import annotations

from pathlib import Path

import pytest

from swift_screening.cpm.models import IdentifierType, PartyRole
from swift_screening.parsing.mx import parser as mx
from swift_screening.parsing.mx.parser import parse_mx
from swift_screening.synth.generator import pacs008

NS = "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08"


def doc(tx_body: str) -> bytes:
    return (
        f'<Document xmlns="{NS}"><FIToFICstmrCdtTrf><GrpHdr><MsgId>M</MsgId></GrpHdr>'
        f"<CdtTrfTxInf><PmtId><EndToEndId>E</EndToEndId></PmtId>"
        f'<IntrBkSttlmAmt Ccy="EUR">1.00</IntrBkSttlmAmt>{tx_body}</CdtTrfTxInf>'
        "</FIToFICstmrCdtTrf></Document>"
    ).encode()


AGENTS = (
    "<DbtrAgt><FinInstnId><BICFI>BANKGB2L</BICFI></FinInstnId></DbtrAgt>"
    "<CdtrAgt><FinInstnId><BICFI>DEUTDEFF</BICFI></FinInstnId></CdtrAgt>"
)


def test_private_and_org_identifiers() -> None:
    xml = doc(
        "<Dbtr><Nm>Person A</Nm><Id><PrvtId><DtAndPlcOfBirth><BirthDt>1964-09-02</BirthDt>"
        "<CityOfBirth>Omsk</CityOfBirth><CtryOfBirth>RU</CtryOfBirth></DtAndPlcOfBirth>"
        "<Othr><Id>P7731942</Id><SchmeNm><Cd>CCPT</Cd></SchmeNm></Othr></PrvtId></Id>"
        "<CtryOfRes>RU</CtryOfRes></Dbtr><DbtrAcct><Id><Othr><Id>ACC-1</Id></Othr></Id></DbtrAcct>"
        + AGENTS
        + "<Cdtr><Nm>Org B</Nm><Id><OrgId><AnyBIC>ZYXBIRTH</AnyBIC>"
        "<LEI>5493001KJTIIGC8Y1R12</LEI><Othr><Id>REG-9</Id></Othr></OrgId></Id></Cdtr>"
        "<CdtrAcct><Id><IBAN>GB00WEST12345698765432</IBAN></Id></CdtrAcct>"
    )
    cpm = parse_mx(xml)
    dbtr = next(p for p in cpm.parties if p.role == PartyRole.ORDERING_CUSTOMER)
    types = {i.type: i for i in dbtr.identifiers}
    assert dbtr.party_type == "individual" and dbtr.country_of_residence == "RU"
    assert types[IdentifierType.DATE_OF_BIRTH].value == "1964-09-02"
    assert types[IdentifierType.PLACE_OF_BIRTH].country == "RU"
    assert types[IdentifierType.PASSPORT].value == "P7731942"
    assert types[IdentifierType.ACCOUNT].value == "ACC-1"
    cdtr = next(p for p in cpm.parties if p.role == PartyRole.BENEFICIARY)
    ctypes = {i.type for i in cdtr.identifiers}
    assert cdtr.party_type == "organisation"
    assert {
        IdentifierType.BIC,
        IdentifierType.LEI,
        IdentifierType.OTHER,
        IdentifierType.IBAN,
    } <= ctypes
    assert "IBAN_CHECKSUM" in {w.code for w in cpm.parse_warnings}


def test_agent_details_and_errors() -> None:
    xml = doc(
        "<IntrmyAgt1><FinInstnId><LEI>LEI0001</LEI><ClrSysMmbId><MmbId>021000021</MmbId>"
        "</ClrSysMmbId><Nm>Some Bank</Nm><PstlAdr><Ctry>CU</Ctry></PstlAdr></FinInstnId>"
        "</IntrmyAgt1><IntrmyAgt2><BrnchId/></IntrmyAgt2>"
        "<DbtrAgt><FinInstnId><BICFI>NOT-A-BIC</BICFI></FinInstnId></DbtrAgt>"
        "<Dbtr><Nm>A</Nm></Dbtr><CdtrAgt><FinInstnId><BICFI>DEUTDEFF</BICFI></FinInstnId>"
        "</CdtrAgt><Cdtr><PstlAdr><Ctry>GB</Ctry></PstlAdr></Cdtr>"
        "<Purp><Cd>GDDS</Cd></Purp><InstrForCdtrAgt><Cd>PHOB</Cd><InstrInf>call</InstrInf>"
        "</InstrForCdtrAgt>"
    )
    cpm = parse_mx(xml)
    inter = [p for p in cpm.parties if p.role == PartyRole.INTERMEDIARY]
    assert inter[0].name == "Some Bank" and inter[0].country == "CU"
    assert {i.type for i in inter[0].identifiers} == {
        IdentifierType.LEI,
        IdentifierType.CLEARING_CODE,
    }
    codes = {w.code for w in cpm.parse_warnings}
    assert {"MISSING_FININSTNID", "INVALID_BIC", "MISSING_NAME"} <= codes
    assert cpm.purpose_codes == ["GDDS"] and cpm.instructions[0].code == "PHOB"


def test_xsd_validation_used_when_schema_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xsd = tmp_path / "pacs.008.001.08.xsd"
    xsd.write_text(
        f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="{NS}" '
        f'xmlns="{NS}" elementFormDefault="qualified">'
        '<xs:element name="Document"><xs:complexType><xs:sequence>'
        '<xs:element name="OnlyThis" type="xs:string"/></xs:sequence></xs:complexType>'
        "</xs:element></xs:schema>"
    )
    monkeypatch.setattr(mx, "XSD_DIR", tmp_path)
    cpm = parse_mx(pacs008())
    assert "XSD_INVALID" in {w.code for w in cpm.parse_warnings if w.severity == "error"}


def test_generic_unsupported_type_collects_bics_and_countries() -> None:
    xml = (
        b'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"><CstmrCdtTrfInitn>'
        b"<PmtInf><DbtrAgt><FinInstnId><BICFI>ZYXBIRTH</BICFI></FinInstnId></DbtrAgt>"
        b"<Cdtr><Nm>Zarkhov</Nm><PstlAdr><Ctry>KP</Ctry></PstlAdr></Cdtr></PmtInf>"
        b"</CstmrCdtTrfInitn></Document>"
    )
    cpm = parse_mx(xml)
    assert any(i.value == "ZYXBIRTH" for p in cpm.parties for i in p.identifiers)
    assert any(p.country == "KP" for p in cpm.parties)


def test_document_inside_envelope_without_app_hdr() -> None:
    inner = pacs008().split("?>", 1)[1]
    cpm = parse_mx(f"<Wrapper>{inner}</Wrapper>")
    assert cpm.message_type == "pacs.008.001.08"


def test_no_document_element() -> None:
    from swift_screening.parsing.errors import ParseError

    with pytest.raises(ParseError):
        parse_mx(b"<Wrapper><Other/></Wrapper>")
