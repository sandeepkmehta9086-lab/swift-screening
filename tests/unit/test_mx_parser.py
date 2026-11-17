"""ISO 20022 MX parsing scenarios B.1 - B.12."""

from __future__ import annotations

import pytest

from swift_screening.cpm.models import IdentifierType, PartyRole
from swift_screening.parsing.errors import ParseError
from swift_screening.parsing.mx.parser import parse_mx
from swift_screening.synth.generator import PartySpec, pacs008, pacs009


def roles(cpm):  # type: ignore[no-untyped-def]
    return [p.role for p in cpm.parties]


def errors(cpm):  # type: ignore[no-untyped-def]
    return {w.code for w in cpm.parse_warnings if w.severity == "error"}


@pytest.mark.scenario("B.1")
def test_valid_pacs008_single_transaction() -> None:
    cpm = parse_mx(
        pacs008(
            debtor=PartySpec(name="Anna Veldkamp", account="NL91ABNA0417164300"),
            creditor=PartySpec(name="John Quillon", country="GB"),
            remittance="Invoice 42",
        )
    )
    assert cpm.message_type == "pacs.008.001.08"
    assert not cpm.has_parse_errors, cpm.parse_warnings
    dbtr = next(p for p in cpm.parties if p.role == PartyRole.ORDERING_CUSTOMER)
    assert dbtr.name == "Anna Veldkamp"
    assert dbtr.provenance[0].path == "MX:CdtTrfTxInf[0]/Dbtr"
    iban = next(i for i in dbtr.identifiers if i.type == IdentifierType.IBAN)
    assert iban.valid and iban.country == "NL"
    cdtr = next(p for p in cpm.parties if p.role == PartyRole.BENEFICIARY)
    assert cdtr.name == "John Quillon" and cdtr.country == "GB"
    assert {
        PartyRole.DEBTOR_AGENT,
        PartyRole.CREDITOR_AGENT,
        PartyRole.INSTRUCTING_AGENT,
        PartyRole.INSTRUCTED_AGENT,
    } <= set(roles(cpm))
    assert cpm.remittance_info[0].text == "Invoice 42"
    assert str(cpm.amount) == "1500.00" and cpm.currency == "EUR"
    assert cpm.uetr == "00000000-0000-0000-0000-000000000001"


@pytest.mark.scenario("B.2")
def test_multiple_transactions_each_mapped() -> None:
    cpm = parse_mx(pacs008(transactions=3, seed=5))
    assert cpm.transaction_count == 3
    benes = [p for p in cpm.parties if p.role == PartyRole.BENEFICIARY]
    assert sorted(p.transaction_index for p in benes) == [0, 1, 2]  # type: ignore[type-var]


@pytest.mark.scenario("B.3")
def test_ultimate_parties() -> None:
    cpm = parse_mx(
        pacs008(
            ultimate_debtor=PartySpec(name="Ultimate Payer"),
            ultimate_creditor=PartySpec(name="Ultimate Payee"),
        )
    )
    names = {p.role: p.name for p in cpm.parties}
    assert names[PartyRole.ULTIMATE_DEBTOR] == "Ultimate Payer"
    assert names[PartyRole.ULTIMATE_CREDITOR] == "Ultimate Payee"


@pytest.mark.scenario("B.4")
def test_intermediary_agents() -> None:
    cpm = parse_mx(pacs008(intermediary="SNCTRUMMXXX"))
    inter = next(p for p in cpm.parties if p.role == PartyRole.INTERMEDIARY)
    assert inter.identifiers[0].value == "SNCTRUMMXXX" and inter.country == "RU"


@pytest.mark.scenario("B.5")
def test_structured_address() -> None:
    cpm = parse_mx(
        pacs008(
            creditor=PartySpec(name="X Y", address=["Mill Lane"], town="SEVASTOPOL", country="UA")
        )
    )
    cdtr = next(p for p in cpm.parties if p.role == PartyRole.BENEFICIARY)
    assert cdtr.address is not None
    assert cdtr.address.town == "SEVASTOPOL" and cdtr.address.country == "UA"
    assert cdtr.country == "UA"


@pytest.mark.scenario("B.6")
def test_unstructured_address_lines() -> None:
    cpm = parse_mx(
        pacs008(
            creditor=PartySpec(name="X Y", address=["12 Mill Lane"], town="TEHRAN", country="IR"),
            structured_address=False,
        )
    )
    cdtr = next(p for p in cpm.parties if p.role == PartyRole.BENEFICIARY)
    assert cdtr.address is not None and cdtr.address.lines == ["12 Mill Lane", "TEHRAN IR"]
    assert "TEHRAN IR" in cdtr.name_lines


@pytest.mark.scenario("B.7")
def test_hybrid_address() -> None:
    xml = pacs008(creditor=PartySpec(name="Hybrid Co", town="PARIS", country="FR")).replace(
        "<TwnNm>PARIS</TwnNm>", "<TwnNm>PARIS</TwnNm><AdrLine>Building 7, Floor 2</AdrLine>"
    )
    cdtr = next(p for p in parse_mx(xml).parties if p.role == PartyRole.BENEFICIARY)
    assert cdtr.address is not None
    assert cdtr.address.town == "PARIS" and cdtr.address.lines == ["Building 7, Floor 2"]


@pytest.mark.scenario("B.8")
def test_remittance_structured_and_unstructured() -> None:
    xml = pacs008(remittance="Ustrd text").replace(
        "</RmtInf>",
        "<Strd><RfrdDocInf><Nb>INV-9</Nb></RfrdDocInf>"
        "<AddtlRmtInf>for Zarkhov</AddtlRmtInf></Strd></RmtInf>",
    )
    texts = [r.text for r in parse_mx(xml).remittance_info]
    assert texts == ["Ustrd text", "INV-9 for Zarkhov"]


def test_instruction_for_next_agent_and_regulatory_reporting() -> None:
    xml = pacs008(instr_for_next_agent="/BNF/PAY ROSNAVTEK").replace(
        "</CdtTrfTxInf>",
        "<RgltryRptg><Dtls><Ctry>IR</Ctry><Inf>goods</Inf></Dtls></RgltryRptg></CdtTrfTxInf>",
    )
    cpm = parse_mx(xml)
    assert cpm.instructions[0].text == "/BNF/PAY ROSNAVTEK"
    assert cpm.regulatory_reporting[0].text == "IR goods"


@pytest.mark.scenario("B.9")
@pytest.mark.parametrize("drop", ["Dbtr", "Cdtr", "IntrBkSttlmAmt"])
def test_structural_validation_failure_is_error(drop: str) -> None:
    cpm = parse_mx(pacs008(drop={drop}))
    assert "XSD_STRUCTURE" in errors(cpm)


def test_bad_amount_and_currency() -> None:
    xml = pacs008().replace('Ccy="EUR">1500.00', 'Ccy="EURO">15,00')
    assert "XSD_STRUCTURE" in errors(parse_mx(xml))


def test_wrong_body_element() -> None:
    xml = pacs008().replace("FIToFICstmrCdtTrf", "SomethingElse")
    assert "XSD_STRUCTURE" in errors(parse_mx(xml))


@pytest.mark.scenario("B.10")
def test_business_application_header() -> None:
    cpm = parse_mx(pacs008(with_app_hdr=True))
    sender = next(p for p in cpm.parties if p.role == PartyRole.SENDER)
    assert sender.identifiers[0].value == "BANKGB2LXXX"
    assert any(p.role == PartyRole.RECEIVER for p in cpm.parties)
    assert not cpm.has_parse_errors


def test_possible_duplicate_flag_in_app_hdr() -> None:
    xml = pacs008(with_app_hdr=True).replace("</CreDt>", "</CreDt><PssblDplct>true</PssblDplct>")
    assert parse_mx(xml).possible_duplicate


@pytest.mark.scenario("B.11")
def test_pacs009_cov_underlying_customers() -> None:
    cpm = parse_mx(
        pacs009(
            cov_debtor=PartySpec(name="Underlying Payer"),
            cov_creditor=PartySpec(name="Underlying Payee"),
        )
    )
    assert cpm.message_type == "pacs.009.001.08.COV"
    assert not cpm.has_parse_errors, cpm.parse_warnings
    seq_b = {p.role: p.name for p in cpm.parties if p.sequence == "B"}
    assert seq_b[PartyRole.ORDERING_CUSTOMER] == "Underlying Payer"
    assert seq_b[PartyRole.BENEFICIARY] == "Underlying Payee"


def test_pacs009_fi_parties_are_institutions() -> None:
    cpm = parse_mx(pacs009(creditor_bic="ZYXBIRTH"))
    bi = next(p for p in cpm.parties if p.role == PartyRole.BENEFICIARY_INSTITUTION)
    assert bi.party_type == "institution" and bi.identifiers[0].value == "ZYXBIRTH"


@pytest.mark.scenario("B.12")
@pytest.mark.parametrize(
    "payload",
    [
        b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]><d>&x;</d>',
        b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><l>&b;</l>',
        b"<a>" * 100 + b"</a>" * 100,  # excessive depth
        b"<Document><unclosed>",
        b"not xml at all",
        b"",
    ],
)
def test_xml_attacks_and_garbage_rejected(payload: bytes) -> None:
    with pytest.raises(ParseError):
        parse_mx(payload)


def test_non_iso_namespace_rejected() -> None:
    with pytest.raises(ParseError):
        parse_mx(b'<Document xmlns="urn:example"><X/></Document>')


def test_oversized_rejected() -> None:
    with pytest.raises(ParseError):
        parse_mx(b"<a>" + b"x" * 6_000_000 + b"</a>")


def test_unsupported_iso_type_is_screened_generically() -> None:
    xml = (
        b'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.056.001.08"><FIToFIPmtCxlReq>'
        b"<Undrlyg><TxInf><OrgnlTxRef><Cdtr><Pty><Nm>Rosnavtek</Nm></Pty></Cdtr></OrgnlTxRef>"
        b"</TxInf></Undrlyg></FIToFIPmtCxlReq></Document>"
    )
    cpm = parse_mx(xml)
    assert "UNSUPPORTED_MESSAGE_TYPE" in errors(cpm)
    assert any(f.text == "Rosnavtek" for f in cpm.unmapped_fields)


@pytest.mark.parametrize("seed", range(50))
def test_golden_generated_pacs008_corpus(seed: int) -> None:
    cpm = parse_mx(
        pacs008(
            seed=seed,
            transactions=1 + seed % 3,
            remittance=f"INV {seed}",
            structured_address=seed % 2 == 0,
        )
    )
    assert not cpm.has_parse_errors, cpm.parse_warnings
