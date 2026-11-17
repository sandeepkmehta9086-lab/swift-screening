"""Geo rules (D.3-D.6, D.11, D.12), policy (E.1-E.6) and extraction unit tests."""

from __future__ import annotations

import pytest

from swift_screening.config.models import (
    AppConfig,
    EmbargoCountry,
    EmbargoRegion,
    TenantConfig,
    Thresholds,
    load_config,
)
from swift_screening.decision.models import Decision
from swift_screening.decision.policy import PolicyContext, evaluate
from swift_screening.decision.reason_codes import ReasonCode
from swift_screening.extraction.subjects import extract_subjects
from swift_screening.lists.model import ListType, WatchlistEntry
from swift_screening.matching.engine import RawHit
from swift_screening.matching.geo import GeoHit, GeoRules
from swift_screening.parsing.mt.parser import parse_mt
from swift_screening.synth.generator import PartySpec, mt103

GEO = GeoRules(
    [
        EmbargoCountry(code="IR", names=["iran", "islamic republic of iran", "iranian"]),
        EmbargoCountry(code="KP", names=["north korea", "dprk"]),
    ],
    [
        EmbargoRegion(id="UA-CRIMEA", names=["crimea", "sevastopol", "simferopol"]),
        EmbargoRegion(id="UA-LUHANSK", names=["luhansk", "lugansk"]),
    ],
)


@pytest.mark.scenario("D.3")
@pytest.mark.scenario("D.4")
@pytest.mark.scenario("D.5")
def test_country_codes() -> None:
    assert GEO.check_code("IR", "x") is not None
    assert GEO.check_code("ir", "x") is not None
    assert GEO.check_code("GB", "x") is None


@pytest.mark.scenario("D.6")
@pytest.mark.parametrize(
    "text,rule",
    [
        ("12 Lenin St, Sevastopol", "UA-CRIMEA"),
        ("Sevastopoll harbour", "UA-CRIMEA"),  # misspelling
        ("Republic of Crimea", "UA-CRIMEA"),
        ("Lugansk region", "UA-LUHANSK"),
    ],
)
def test_region_names_in_text(text: str, rule: str) -> None:
    assert rule in {h.rule_id for h in GEO.check_text(text, "p")}


@pytest.mark.scenario("D.11")
@pytest.mark.parametrize(
    "text,code",
    [
        ("Goods shipped to Iran via Dubai", "IR"),
        ("payment for IRANIAN buyer", "IR"),
        ("delivery to North Korea", "KP"),
        ("DPRK trade office", "KP"),
    ],
)
def test_country_names_in_free_text(text: str, code: str) -> None:
    assert code in {h.rule_id for h in GEO.check_text(text, "p")}


@pytest.mark.scenario("D.12")
@pytest.mark.parametrize(
    "text",
    [
        "FIRST NATIONAL BANK",
        "IRREVOCABLE CREDIT",
        "KPMG AUDIT FEE",
        "Cimea Ltd",
        "Korean food import",
    ],
)
def test_no_substring_or_code_false_positives(text: str) -> None:
    assert GEO.check_text(text, "p") == []


def test_demonym_spelling_variant_is_flagged_on_purpose() -> None:
    # "Irani" is a common spelling of "Iranian"; recall-first means we flag it for review.
    assert {h.rule_id for h in GEO.check_text("Irani Street Cafe", "p")} == {"IR"}


# ------------------------------------------------------------------ policy
def _tenant(**kw: object) -> TenantConfig:
    return TenantConfig(tenant_id="p", **kw)  # type: ignore[arg-type]


def _hit(score: float, methods: set[str] | None = None) -> RawHit:
    from swift_screening.extraction.subjects import Subject

    return RawHit(
        Subject("name", "x", "MT:59"), 0, None, score, methods or {"fuzzy"}, {"fuzzy"}, "x"
    )


def _entry(list_type: ListType = ListType.SANCTIONS) -> WatchlistEntry:
    return WatchlistEntry(list_id="L", entry_id="1", name="X", list_type=list_type)


@pytest.mark.scenario("E.1")
def test_no_hits_pass_with_no_match() -> None:
    out = evaluate([], [], [], PolicyContext(), _tenant())
    assert out.decision == Decision.PASS and out.reasons == [ReasonCode.NO_MATCH]


def test_hit_below_review_threshold_is_pass() -> None:
    out = evaluate([_hit(0.80)], [_entry()], [], PolicyContext(), _tenant())
    assert out.decision == Decision.PASS


@pytest.mark.scenario("E.2")
def test_between_thresholds_review() -> None:
    out = evaluate([_hit(0.9)], [_entry()], [], PolicyContext(), _tenant())
    assert out.decision == Decision.REVIEW and ReasonCode.HIT_SANCTIONS in out.reasons


@pytest.mark.scenario("E.3")
def test_above_block_threshold_block() -> None:
    out = evaluate([_hit(0.99)], [_entry()], [], PolicyContext(), _tenant())
    assert out.decision == Decision.BLOCK


def test_identifier_hit_action_configurable() -> None:
    h = _hit(1.0, {"exact_id"})
    assert evaluate([h], [_entry()], [], PolicyContext(), _tenant()).decision == Decision.BLOCK
    t = _tenant(identifier_hit_action="REVIEW")
    out = evaluate([h], [_entry()], [], PolicyContext(), t)
    assert out.decision == Decision.REVIEW and ReasonCode.HIT_IDENTIFIER in out.reasons


@pytest.mark.scenario("E.4")
def test_pep_only_is_review_even_on_exact() -> None:
    out = evaluate([_hit(1.0)], [_entry(ListType.PEP)], [], PolicyContext(), _tenant())
    assert out.decision == Decision.REVIEW and out.reasons == [ReasonCode.HIT_PEP]


@pytest.mark.scenario("E.5")
def test_multiple_hits_most_severe_wins() -> None:
    entries = [_entry(ListType.PEP), _entry()]
    hits = [_hit(0.9), RawHit(_hit(0.99).subject, 1, None, 0.99, {"fuzzy"}, set(), "x")]
    hits[0].entry_idx = 0
    out = evaluate(hits, entries, [], PolicyContext(), _tenant())
    assert out.decision == Decision.BLOCK
    assert {ReasonCode.HIT_PEP, ReasonCode.HIT_SANCTIONS} <= set(out.reasons)


@pytest.mark.scenario("E.6")
def test_tenant_thresholds_respected() -> None:
    strict = _tenant(thresholds=Thresholds(review=0.75, block=1.01))
    assert evaluate([_hit(0.78)], [_entry()], [], PolicyContext(), strict).decision == "REVIEW"
    assert evaluate([_hit(1.0)], [_entry()], [], PolicyContext(), strict).decision == "REVIEW"
    assert strict.config_version != _tenant().config_version


def test_thresholds_validated() -> None:
    with pytest.raises(ValueError):
        Thresholds(review=0.9, block=0.8)


@pytest.mark.parametrize(
    "ctx,reason",
    [
        (PolicyContext(parse_errors=True), ReasonCode.PARSE_FAILED),
        (PolicyContext(missing_lists=["X"]), ReasonCode.LIST_UNAVAILABLE),
        (PolicyContext(stale_lists=["X"]), ReasonCode.LIST_STALE),
        (PolicyContext(forced_reasons=[ReasonCode.TIMEOUT]), ReasonCode.TIMEOUT),
    ],
)
def test_fail_closed_contexts_force_review(ctx: PolicyContext, reason: ReasonCode) -> None:
    out = evaluate([], [], [], ctx, _tenant())
    assert out.decision == Decision.REVIEW and reason in out.reasons
    assert ReasonCode.NO_MATCH not in out.reasons


def test_stale_flag_mode_does_not_hold() -> None:
    t = _tenant()
    t.lists.stale_action = "flag"
    out = evaluate([], [], [], PolicyContext(stale_lists=["X"]), t)
    assert out.decision == Decision.PASS and ReasonCode.LIST_STALE in out.reasons


def test_geo_actions() -> None:
    g = [
        GeoHit("IR", "country", "BLOCK", "p", "IR", "IR", "code"),
        GeoHit("UA-CRIMEA", "region", "REVIEW", "p", "x", "x", "name"),
    ]
    out = evaluate([], [], g, PolicyContext(), _tenant())
    assert out.decision == Decision.BLOCK
    assert {ReasonCode.EMBARGO_COUNTRY, ReasonCode.EMBARGO_REGION} <= set(out.reasons)


# ------------------------------------------------------------------ extraction
def test_extraction_produces_names_lines_identifiers_countries() -> None:
    cpm = parse_mt(
        mt103(
            beneficiary=PartySpec(name="ANNA VELDKAMP", address=["NEXT LINE"], country="NL"),
            beneficiary_option="F",
            remittance="ON BEHALF OF ZARKHOV INDUSTRIAL",
            field72="/BNF/ROSNAVTEK EXPORT",
        )
    )
    subs = extract_subjects(cpm)
    names = {s.value for s in subs if s.kind == "name"}
    assert {"ANNA VELDKAMP", "ZARKHOV INDUSTRIAL", "ROSNAVTEK EXPORT"} <= names
    assert any(s.kind == "identifier" and s.id_type == "BIC" for s in subs)
    assert any(s.kind == "country" and s.value == "NL" for s in subs)
    assert any(s.kind == "text" and "NEXT LINE" in s.value for s in subs)
    hidden = next(s for s in subs if s.value == "ZARKHOV INDUSTRIAL")
    assert hidden.origin == "free_text_rule" and hidden.path.startswith("MT:70")


@pytest.mark.parametrize(
    "phrase",
    [
        "on behalf of",
        "OBO",
        "F.F.C.",
        "for further credit to",
        "in favour of",
        "IFO",
        "ultimate beneficiary",
    ],
)
def test_hidden_party_phrases(phrase: str) -> None:
    cpm = parse_mt(mt103(remittance=f"PAYMT {phrase} QUILLFEATHER LOGISTICS"))
    names = {s.value for s in extract_subjects(cpm) if s.kind == "name"}
    assert "QUILLFEATHER LOGISTICS" in names


def test_example_config_loads() -> None:
    from pathlib import Path

    cfg = load_config(Path(__file__).resolve().parents[2] / "config" / "tenants.example.yaml")
    assert isinstance(cfg, AppConfig)
    demo = cfg.tenant("demo")
    assert demo is not None and demo.embargo_countries and demo.embargo_regions
    assert cfg.tenant_for_key("demo-key-change-me") is demo
    assert cfg.tenant_for_key("wrong") is None


def test_duplicate_api_key_across_tenants_rejected() -> None:
    with pytest.raises(ValueError):
        AppConfig(
            tenants=[
                TenantConfig(tenant_id="a", api_key_sha256=["k"]),
                TenantConfig(tenant_id="b", api_key_sha256=["k"]),
            ]
        )
