"""MT parsing scenarios A.1 - A.20."""

from __future__ import annotations

import time

import pytest

from swift_screening.cpm.models import IdentifierType, PartyRole
from swift_screening.parsing.errors import ParseError
from swift_screening.parsing.mt.parser import parse_mt
from swift_screening.synth.generator import PartySpec, mt103, mt202, mt202cov

HDR = "{1:F01BANKGB2LAXXX0000000000}{2:I103DEUTDEFFXXXXN}{3:{108:REF}{121:e1f2a3b4-0000-4000-8000-000000000001}}"


def msg(body: str, hdr: str = HDR, trailer: str = "{5:{CHK:ABC}}") -> str:
    return hdr + "{4:\n" + body.strip("\n") + "\n-}" + trailer


MT103_BODY = """
:20:REF123
:23B:CRED
:32A:261115EUR12500,50
:50K:/GB82WEST12345698765432
JOHN QUILLON
1 HARBOUR ROAD
LONDON GB
:52A:BANKGB2L
:57A:DEUTDEFFXXX
:59:/DE89370400440532013000
ANNA VELDKAMP
MAIN STREET 5
FRANKFURT
:70:INVOICE 4471 MACHINE PARTS
:71A:SHA
"""


def party(cpm, role, seq=None):  # type: ignore[no-untyped-def]
    return next(p for p in cpm.parties if p.role == role and p.sequence == seq)


def errors(cpm):  # type: ignore[no-untyped-def]
    return {w.code for w in cpm.parse_warnings if w.severity == "error"}


@pytest.mark.scenario("A.1")
def test_valid_mt103_50k_59() -> None:
    cpm = parse_mt(msg(MT103_BODY))
    assert cpm.message_type == "MT103"
    assert not cpm.has_parse_errors, cpm.parse_warnings
    oc = party(cpm, PartyRole.ORDERING_CUSTOMER)
    assert oc.name == "JOHN QUILLON"
    assert oc.name_lines == ["JOHN QUILLON", "1 HARBOUR ROAD", "LONDON GB"]
    iban = next(i for i in oc.identifiers if i.type == IdentifierType.IBAN)
    assert iban.valid is True and iban.country == "GB"
    ben = party(cpm, PartyRole.BENEFICIARY)
    assert ben.name == "ANNA VELDKAMP"
    assert cpm.amount is not None and str(cpm.amount) == "12500.50"
    assert cpm.currency == "EUR"
    assert cpm.value_date is not None and cpm.value_date.isoformat() == "2026-11-15"
    assert cpm.message_id == "REF123"
    # sender / receiver from headers
    assert party(cpm, PartyRole.SENDER).identifiers[0].value == "BANKGB2LXXX"
    assert party(cpm, PartyRole.RECEIVER).identifiers[0].value == "DEUTDEFFXXX"
    assert cpm.remittance_info[0].text == "INVOICE 4471 MACHINE PARTS"
    assert cpm.remittance_info[0].provenance.path == "MT:70"


@pytest.mark.scenario("A.2")
def test_50f_structured_ordering_customer() -> None:
    body = MT103_BODY.replace(
        ":50K:/GB82WEST12345698765432\nJOHN QUILLON\n1 HARBOUR ROAD\nLONDON GB",
        ":50F:/GB82WEST12345698765432\n1/QUILLON, JOHN\n2/1 HARBOUR ROAD\n3/GB/LONDON\n"
        "4/19710314\n5/YE/SANAA\n7/YE/A1234567",
    )
    cpm = parse_mt(msg(body))
    oc = party(cpm, PartyRole.ORDERING_CUSTOMER)
    assert oc.name == "QUILLON, JOHN"
    assert oc.country == "GB" and oc.address and oc.address.town == "LONDON"
    types = {i.type: i for i in oc.identifiers}
    assert types[IdentifierType.DATE_OF_BIRTH].value == "1971-03-14"
    assert types[IdentifierType.PLACE_OF_BIRTH].country == "YE"
    assert types[IdentifierType.NATIONAL_ID].value == "A1234567"
    assert oc.party_type == "individual"
    assert not cpm.has_parse_errors


def test_50f_with_party_identifier_code() -> None:
    body = MT103_BODY.replace(
        ":50K:/GB82WEST12345698765432\nJOHN QUILLON\n1 HARBOUR ROAD\nLONDON GB",
        ":50F:CCPT/GB/K1234567\n1/JOHN QUILLON\n3/GB/LONDON",
    )
    oc = party(parse_mt(msg(body)), PartyRole.ORDERING_CUSTOMER)
    passport = next(i for i in oc.identifiers if i.type == IdentifierType.PASSPORT)
    assert passport.value == "K1234567" and passport.country == "GB"


@pytest.mark.scenario("A.3")
def test_50a_bic_only() -> None:
    body = MT103_BODY.replace(
        ":50K:/GB82WEST12345698765432\nJOHN QUILLON\n1 HARBOUR ROAD\nLONDON GB",
        ":50A:/12345678\nSNCTRUMM",
    )
    oc = party(parse_mt(msg(body)), PartyRole.ORDERING_CUSTOMER)
    bic = next(i for i in oc.identifiers if i.type == IdentifierType.BIC)
    assert bic.value == "SNCTRUMM" and bic.country == "RU" and oc.country == "RU"


@pytest.mark.scenario("A.4")
def test_59f_structured_beneficiary() -> None:
    cpm = parse_mt(
        mt103(
            beneficiary=PartySpec(name="ANNA VELDKAMP", country="NL", town="AMSTERDAM"),
            beneficiary_option="F",
        )
    )
    ben = party(cpm, PartyRole.BENEFICIARY)
    assert ben.name == "ANNA VELDKAMP" and ben.country == "NL"
    assert not cpm.has_parse_errors


@pytest.mark.scenario("A.5")
def test_59a_bic_beneficiary() -> None:
    body = MT103_BODY.replace(
        ":59:/DE89370400440532013000\nANNA VELDKAMP\nMAIN STREET 5\nFRANKFURT",
        ":59A:/DE89370400440532013000\nZYXBIRTH",
    )
    ben = party(parse_mt(msg(body)), PartyRole.BENEFICIARY)
    assert any(i.type == IdentifierType.BIC and i.value == "ZYXBIRTH" for i in ben.identifiers)


@pytest.mark.scenario("A.6")
def test_all_agent_fields_extracted_with_roles() -> None:
    extra = """
:53A:BANKGB2LXXX
:54A:CHASUS33XXX
:55A:INGBNL2AXXX
:56A:UBSWCHZH80A
:57D:SOME BANK
ROAD 1
CITY
"""
    body = MT103_BODY.replace(":57A:DEUTDEFFXXX", "").replace(":59:", extra.strip() + "\n:59:")
    cpm = parse_mt(msg(body))
    roles = {p.role for p in cpm.parties}
    for r in (
        PartyRole.ORDERING_INSTITUTION,
        PartyRole.SENDERS_CORRESPONDENT,
        PartyRole.RECEIVERS_CORRESPONDENT,
        PartyRole.THIRD_REIMBURSEMENT,
        PartyRole.INTERMEDIARY,
        PartyRole.ACCOUNT_WITH_INSTITUTION,
    ):
        assert r in roles, r
    awi = party(cpm, PartyRole.ACCOUNT_WITH_INSTITUTION)
    assert awi.name == "SOME BANK" and awi.name_lines[1:] == ["ROAD 1", "CITY"]


@pytest.mark.scenario("A.7")
def test_mt202_fi_to_fi() -> None:
    cpm = parse_mt(mt202(beneficiary_institution="ZYXBIRTH", intermediary="SNCTRUMMXXX"))
    assert cpm.message_type == "MT202"
    assert not cpm.has_parse_errors, cpm.parse_warnings
    bi = party(cpm, PartyRole.BENEFICIARY_INSTITUTION)
    assert bi.identifiers[0].value == "ZYXBIRTH"
    assert party(cpm, PartyRole.INTERMEDIARY).identifiers[0].value == "SNCTRUMMXXX"


@pytest.mark.scenario("A.8")
def test_mt202cov_sequence_b_underlying_customers() -> None:
    cpm = parse_mt(
        mt202cov(
            ordering=PartySpec(name="UNDERLYING PAYER"),
            beneficiary=PartySpec(name="UNDERLYING PAYEE"),
        )
    )
    assert cpm.message_type == "MT202COV"
    assert not cpm.has_parse_errors, cpm.parse_warnings
    oc = party(cpm, PartyRole.ORDERING_CUSTOMER, "B")
    ben = party(cpm, PartyRole.BENEFICIARY, "B")
    assert oc.name == "UNDERLYING PAYER" and ben.name == "UNDERLYING PAYEE"
    assert oc.provenance[0].path == "MT:B/50K"


def test_mt202cov_missing_sequence_b_beneficiary_is_error() -> None:
    raw = mt202cov(ordering=PartySpec(name="A B"), beneficiary=PartySpec(name="C D"))
    raw = raw.split(":59F:")[0] + "-}"
    assert "MISSING_MANDATORY_FIELD" in errors(parse_mt(raw))


@pytest.mark.scenario("A.9")
def test_field_70_multiline() -> None:
    body = MT103_BODY.replace(
        ":70:INVOICE 4471 MACHINE PARTS", ":70:/INV/4471\nMACHINE PARTS\nPO 99812"
    )
    cpm = parse_mt(msg(body))
    assert cpm.remittance_info[0].text == "/INV/4471 MACHINE PARTS PO 99812"


@pytest.mark.scenario("A.10")
def test_field_72_codewords_and_continuation() -> None:
    body = (
        MT103_BODY
        + ":72:/INS/SNCTRUMM\n/BNF/ROSNAVTEK EXPORT\n//PAYMENT FOR GOODS\n/ACC/CREDIT AT ONCE\n"
    )
    cpm = parse_mt(msg(body))
    codes = {i.code: i.text for i in cpm.instructions}
    assert codes["INS"] == "SNCTRUMM"
    assert codes["BNF"] == "ROSNAVTEK EXPORT PAYMENT FOR GOODS"
    assert codes["ACC"] == "CREDIT AT ONCE"


@pytest.mark.scenario("A.11")
def test_field_77b_regulatory_reporting() -> None:
    body = MT103_BODY + ":77B:/BENEFRES/IR//TEHRAN\n"
    cpm = parse_mt(msg(body))
    rr = cpm.regulatory_reporting[0]
    assert rr.code == "BENEFRES" and rr.text.startswith("IR//")


@pytest.mark.scenario("A.12")
def test_missing_mandatory_field_is_error() -> None:
    body = "\n".join(
        line for line in MT103_BODY.strip().splitlines() if not line.startswith(":71A:")
    )
    cpm = parse_mt(msg(body))
    assert "MISSING_MANDATORY_FIELD" in errors(cpm)


def test_missing_beneficiary_is_error() -> None:
    body = MT103_BODY.split(":59:")[0] + ":71A:SHA"
    assert "MISSING_MANDATORY_FIELD" in errors(parse_mt(msg(body)))


@pytest.mark.scenario("A.13")
def test_line_too_long_and_too_many_lines_warn_but_keep_content() -> None:
    long_name = "X" * 40
    body = MT103_BODY.replace("ANNA VELDKAMP", long_name).replace(
        "FRANKFURT", "FRANKFURT\nLINE5\nLINE6"
    )
    cpm = parse_mt(msg(body))
    codes = {w.code for w in cpm.parse_warnings}
    assert {"LINE_TOO_LONG", "TOO_MANY_LINES"} <= codes
    ben = party(cpm, PartyRole.BENEFICIARY)
    assert ben.name == long_name and "LINE6" in ben.name_lines


@pytest.mark.scenario("A.14")
def test_non_x_charset_characters_warn_and_are_kept() -> None:
    body = MT103_BODY.replace("ANNA VELDKAMP", "ANNA VËLDKAMP & CO")
    cpm = parse_mt(msg(body))
    assert "CHARSET" in {w.code for w in cpm.parse_warnings}
    assert party(cpm, PartyRole.BENEFICIARY).name == "ANNA VËLDKAMP & CO"


@pytest.mark.scenario("A.15")
@pytest.mark.parametrize(
    "hdr,code",
    [
        ("{1:F01BANKGB2L0000000000}{2:I103DEUTDEFFXXXXN}", "INVALID_BLOCK1"),
        ("{1:F01BANKGB2LAXXX0000000000}{2:X103}", "INVALID_BLOCK2"),
        ("{2:I103DEUTDEFFXXXXN}", "MISSING_BLOCK1"),
        ("{1:F01BANKGB2LAXXX0000000000}", "MISSING_BLOCK2"),
    ],
)
def test_malformed_or_missing_header_blocks(hdr: str, code: str) -> None:
    cpm = parse_mt(msg(MT103_BODY, hdr=hdr))
    assert code in errors(cpm)


def test_body_only_message_is_parsed_but_flagged() -> None:
    cpm = parse_mt(MT103_BODY.strip())
    assert "MISSING_HEADER_BLOCKS" in errors(cpm)
    assert cpm.message_type == "MT103"
    assert party(cpm, PartyRole.BENEFICIARY).name == "ANNA VELDKAMP"


def test_output_message_block2_swaps_sender() -> None:
    hdr = "{1:F01DEUTDEFFAXXX0000000000}{2:O1031200261115BANKGB2LAXXX00000000002611151201N}"
    cpm = parse_mt(msg(MT103_BODY, hdr=hdr))
    assert party(cpm, PartyRole.SENDER).identifiers[0].value == "BANKGB2LXXX"
    assert party(cpm, PartyRole.RECEIVER).identifiers[0].value == "DEUTDEFFXXX"
    assert not errors(cpm)


@pytest.mark.scenario("A.16")
def test_block3_uetr() -> None:
    assert parse_mt(msg(MT103_BODY)).uetr == "e1f2a3b4-0000-4000-8000-000000000001"


@pytest.mark.scenario("A.17")
def test_block5_possible_duplicate() -> None:
    assert parse_mt(msg(MT103_BODY, trailer="{5:{CHK:ABC}{PDE:}}")).possible_duplicate
    assert not parse_mt(msg(MT103_BODY)).possible_duplicate


@pytest.mark.scenario("A.18")
@pytest.mark.parametrize("raw", ["", "   \n  "])
def test_empty_input_raises(raw: str) -> None:
    with pytest.raises(ParseError):
        parse_mt(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "hello world, this is not swift",
        "{1:F01BANKGB2LAXXX0000000000}{2:I103DEUTDEFFXXXXN}{4:\n:20:X\n",  # unterminated
        "{1:F01BANKGB2LAXXX0000000000",  # unclosed block
        "{4:\n-}",  # no fields
    ],
)
def test_garbage_raises_parse_error(raw: str) -> None:
    with pytest.raises(ParseError):
        parse_mt(raw)


@pytest.mark.scenario("A.19")
def test_oversized_and_field_repetition_bounded() -> None:
    with pytest.raises(ParseError):
        parse_mt("{4:\n" + ":70:AAAA\n" * 20_000 + "-}")
    start = time.perf_counter()
    big = msg(MT103_BODY + ":72:" + "\n".join(["/REC/" + "X" * 30] * 400))
    parse_mt(big)
    assert time.perf_counter() - start < 1.0


@pytest.mark.scenario("A.20")
def test_unknown_field_is_kept_as_unmapped_free_text() -> None:
    cpm = parse_mt(msg(MT103_BODY + ":99Z:HIDDEN PARTY ZARKHOV INDUSTRIAL\n"))
    assert cpm.unmapped_fields[0].text == "HIDDEN PARTY ZARKHOV INDUSTRIAL"
    assert cpm.unmapped_fields[0].provenance.path == "MT:99Z"
    assert "UNKNOWN_FIELD" in {w.code for w in cpm.parse_warnings}


def test_invalid_option_for_tag_is_error() -> None:
    body = MT103_BODY.replace(":59:", ":59K:")
    assert "INVALID_OPTION" in errors(parse_mt(msg(body)))


def test_invalid_bic_in_option_a_is_error() -> None:
    body = MT103_BODY.replace(":52A:BANKGB2L", ":52A:NOTABIC!")
    assert "INVALID_BIC" in errors(parse_mt(msg(body)))


def test_invalid_32a_is_error() -> None:
    body = MT103_BODY.replace("261115EUR12500,50", "26111EUR12500.50")
    assert "INVALID_32A" in errors(parse_mt(msg(body)))


def test_unsupported_message_type_is_error_but_content_kept() -> None:
    hdr = "{1:F01BANKGB2LAXXX0000000000}{2:I999DEUTDEFFXXXXN}"
    cpm = parse_mt(msg(":20:FREE\n:79:PLEASE PAY ZARKHOV INDUSTRIAL", hdr=hdr))
    assert "UNSUPPORTED_MESSAGE_TYPE" in errors(cpm)
    assert cpm.unmapped_fields[0].text == "PLEASE PAY ZARKHOV INDUSTRIAL"


def test_crlf_line_endings() -> None:
    cpm = parse_mt(msg(MT103_BODY).replace("\n", "\r\n"))
    assert not cpm.has_parse_errors
    assert party(cpm, PartyRole.BENEFICIARY).name == "ANNA VELDKAMP"


def test_invalid_iban_checksum_is_warning_only() -> None:
    body = MT103_BODY.replace("GB82WEST12345698765432", "GB00WEST12345698765432")
    cpm = parse_mt(msg(body))
    assert "IBAN_CHECKSUM" in {w.code for w in cpm.parse_warnings}
    assert not cpm.has_parse_errors


@pytest.mark.parametrize("seed", range(50))
def test_golden_generated_mt103_corpus_parses_cleanly(seed: int) -> None:
    cpm = parse_mt(mt103(seed=seed, remittance=f"INVOICE {seed}"))
    assert not cpm.has_parse_errors, cpm.parse_warnings
    assert party(cpm, PartyRole.BENEFICIARY).name
