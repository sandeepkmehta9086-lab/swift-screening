"""False-positive controls found by validating against the real OFAC / UN / EU lists.

Before these rules, every common name tested against the live lists alerted ("John Smith" ->
alias "John", "Maria Garcia" -> vessel "MARIA", "1 Market Square" -> "JSC Square", BIC lines
-> "BANK GPB JSC"). Each rule keeps the true-hit case working (asserted alongside).
"""

from __future__ import annotations

import pytest

from swift_screening.config.models import TenantConfig
from swift_screening.decision.models import Decision
from swift_screening.service import ScreeningService
from swift_screening.synth.generator import PartySpec, mt103, pacs008


def ids(r) -> set[str]:  # type: ignore[no-untyped-def]
    return {h.matched_entry.id for h in r.hits}


@pytest.fixture
def mt(service: ScreeningService, tenant: TenantConfig):  # type: ignore[no-untyped-def]
    return lambda msg: service.screen_mt(msg, tenant)


@pytest.fixture
def mx(service: ScreeningService, tenant: TenantConfig):  # type: ignore[no-untyped-def]
    return lambda msg: service.screen_mx(msg, tenant)


# --------------------------------------------------- one-word list names in context
def test_common_word_in_address_does_not_hit(mt) -> None:  # type: ignore[no-untyped-def]
    raw = "/GB82WEST12345698765432\nANNA VELDKAMP\n1 MARKET SQUARE\nLONDON"
    assert "FX-1010" not in ids(mt(mt103(raw_beneficiary=raw)))


def test_one_word_name_as_whole_field_still_hits(mt) -> None:  # type: ignore[no-untyped-def]
    r = mt(mt103(beneficiary=PartySpec(name="SQUARE")))
    assert "FX-1010" in ids(r) and r.decision == Decision.REVIEW  # one word: never auto-BLOCK


def test_full_name_with_legal_form_in_text_hits(mt) -> None:  # type: ignore[no-untyped-def]
    assert "FX-1010" in ids(mt(mt103(remittance="PAYMENT TO JSC SQUARE FOR STEEL")))


def test_distinctive_one_word_name_in_context_still_hits(mt, mx) -> None:  # type: ignore[no-untyped-def]
    assert "FX-1002" in ids(mt(mt103(beneficiary=PartySpec(name="ROSNAVTEK TRADING HOUSE"))))
    assert "FX-1002" in ids(mx(pacs008(remittance="Invoice 9 for Rosnavtek, March")))
    assert "FX-1002" in ids(mt(mt103(remittance="ROSNAVTEKOIL SUPPLY")))


def test_one_word_exact_name_reviews_not_blocks(mt) -> None:  # type: ignore[no-untyped-def]
    r = mt(mt103(beneficiary=PartySpec(name="ROSNAVTEK")))
    assert "FX-1002" in ids(r) and r.decision == Decision.REVIEW


# --------------------------------------------------- vessels
def test_vessel_name_does_not_match_people(mt) -> None:  # type: ignore[no-untyped-def]
    r = mt(mt103(beneficiary=PartySpec(name="MARIA GARCIA")))
    assert "FX-2002" not in ids(r) and r.decision == Decision.PASS


@pytest.mark.parametrize(
    "text", ["FREIGHT MV MARIA", "CARGO ON M/V MARIA", "VESSEL MARIA DEMURRAGE"]
)
def test_vessel_name_in_vessel_context_hits(mt, text: str) -> None:  # type: ignore[no-untyped-def]
    assert "FX-2002" in ids(mt(mt103(remittance=text)))


# --------------------------------------------------- weak aliases
def test_weak_alias_not_matched_inside_longer_name(mt) -> None:  # type: ignore[no-untyped-def]
    r = mt(mt103(beneficiary=PartySpec(name="OMAR FAROUK")))
    assert "FX-0011" not in ids(r) and r.decision == Decision.PASS
    assert "FX-0011" not in ids(mt(mt103(remittance="GIFT FROM OMAR")))


def test_weak_alias_exact_whole_field_reviews(mt) -> None:  # type: ignore[no-untyped-def]
    r = mt(mt103(beneficiary=PartySpec(name="OMAR")))
    assert "FX-0011" in ids(r) and r.decision == Decision.REVIEW


def test_primary_name_still_hits_and_blocks(mt) -> None:  # type: ignore[no-untyped-def]
    r = mt(mt103(beneficiary=PartySpec(name="KASSIM OMAR BAKHTARI")))
    assert "FX-0011" in ids(r) and r.decision == Decision.BLOCK


# --------------------------------------------------- BIC lines and long names
def test_bic_lines_not_screened_as_names(mt) -> None:  # type: ignore[no-untyped-def]
    r = mt(mt103(seed=1))  # 52A BANKGB2L / 57A DEUTDEFFXXX lines
    assert all(not h.field.startswith(("MT:52A", "MT:57A")) for h in r.hits)


def test_small_share_of_a_long_name_is_not_a_match(mt) -> None:  # type: ignore[no-untyped-def]
    # "Kavkaz" alone is 1 of 3 distinctive words of "Kavkaz Precision Machinery JSC"
    r = mt(mt103(beneficiary=PartySpec(name="KAVKAZ RESTAURANT GROUP")))
    assert "FX-1008" not in ids(r)


def test_country_code_in_address_line_is_not_name_evidence() -> None:
    """Live-list finding: "SINGAPORE SG" matched "SG-DEVELOPMENT, AO" (generic word + code)."""
    from swift_screening.matching.normalize import normalize_name
    from swift_screening.matching.similarity import name_similarity

    entry = normalize_name("SG-DEVELOPMENT, AO")
    for line in ["SINGAPORE SG", "SG", "MAIN ROAD 5 SINGAPORE SG"]:
        assert name_similarity(normalize_name(line), entry).score < 0.82, line
    assert name_similarity(normalize_name("SG DEVELOPMENT"), entry).score >= 0.82
    # names made only of short words still match
    assert name_similarity(normalize_name("Li Wei"), normalize_name("Wei Li")).score >= 0.82
