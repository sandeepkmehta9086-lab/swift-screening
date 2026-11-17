"""End-to-end screening scenarios through ScreeningService (parse -> match -> decide)."""

from __future__ import annotations

import pytest

from swift_screening.config.models import TenantConfig
from swift_screening.decision.models import Decision, ScreeningResult
from swift_screening.lists.store import ListStore
from swift_screening.service import EntityRequest, ScreeningService
from swift_screening.synth.generator import PartySpec, mt103, mt202, mt202cov, pacs008, pacs009


def bene(name: str, **kw: object) -> str:
    return mt103(beneficiary=PartySpec(name=name), **kw)  # type: ignore[arg-type]


def hit_ids(r: ScreeningResult) -> set[str]:
    return {h.matched_entry.id for h in r.hits}


def assert_hit(r: ScreeningResult, entry_id: str, at_least: Decision = Decision.REVIEW) -> None:
    assert entry_id in hit_ids(r), (r.decision, [(h.matched_entry.id, h.score) for h in r.hits])
    assert r.decision.severity >= at_least.severity


@pytest.fixture
def screen(service: ScreeningService, tenant: TenantConfig):  # type: ignore[no-untyped-def]
    def run(msg: str) -> ScreeningResult:
        if msg.lstrip().startswith("<"):
            return service.screen_mx(msg, tenant)
        return service.screen_mt(msg, tenant)

    return run


# ------------------------------------------------------------------ baseline
def test_clean_messages_pass(screen) -> None:  # type: ignore[no-untyped-def]
    for seed in range(20):
        r = screen(mt103(seed=seed, remittance=f"INVOICE {seed} OFFICE SUPPLIES"))
        assert r.decision == Decision.PASS, (seed, r.reason_codes, [h.explanation for h in r.hits])
        assert r.reason_codes == ["NO_MATCH"]
        assert r.list_versions.keys() == {"FIXTURE_SANCTIONS", "FIXTURE_PEP", "FIXTURE_INTERNAL"}


# ------------------------------------------------------------------ C. names
@pytest.mark.scenario("C.1")
def test_exact_name_blocks(screen) -> None:  # type: ignore[no-untyped-def]
    r = screen(bene("Vladimir Sergeyevich Kornilov"))
    assert_hit(r, "FX-0002", Decision.BLOCK)
    h = r.hits[0]
    assert h.field == "MT:59" and h.role == "beneficiary" and h.score == 1.0
    assert "FX-0002" in h.explanation and "MT:59" in h.explanation


@pytest.mark.parametrize(
    "scenario,name,entry",
    [
        ("C.2", "vladimir   SERGEYEVICH, kornilov.", "FX-0002"),
        ("C.3", "Johann Friedrich Muller-Brandt", "FX-0008"),
        ("C.3", "Jöhann Friedrích Müller Brandt", "FX-0008"),
        ("C.4", "Vladimir Sergeyevich Kornilvo", "FX-0002"),
        ("C.4", "Natalia Igorevna Vorontsava", "FX-0010"),
        ("C.5", "Kornilov Vladimir Sergeyevich", "FX-0002"),
        ("C.5", "Song Kim Chol", "FX-0007"),
        ("C.6", "Vladimir Kornilov", "FX-0002"),
        ("C.6", "Hussein Ibrahim Ali Mansour", "FX-0006"),
        ("C.7", "V. S. Kornilov", "FX-0002"),
        ("C.7", "M. Al-Rashid", "FX-0001"),
        ("C.8", "Sheikh Mohammed Khalid Al-Rashid", "FX-0001"),
        ("C.8", "Dr Natalia Vorontsova", "FX-0010"),
        ("C.9", "Mohammed Khalid El Rashid", "FX-0001"),
        ("C.9", "Abdel Rahman Yusuf Haddad", "FX-0005"),
        ("C.9", "Abdulrahman Youssef Haddad", "FX-0005"),
        ("C.9", "Usama Faris ibn Talal", "FX-0009"),
        ("C.10", "Muhammad Khaled Al Rasheed", "FX-0001"),
        ("C.10", "Evgenii Pavlovich Dragunov", "FX-0003"),
        ("C.10", "Husain Ali Mansur", "FX-0006"),
        ("C.12", "Abu Khalid al-Rashidi", "FX-0001"),
        ("C.12", "ZIH Group", "FX-1001"),
        ("C.13", "Zarkhov Industrial Holdings Limited", "FX-1001"),
        ("C.13", "Zarkhov Industrial OOO", "FX-1001"),
        ("C.13", "Kavkaz Precision Machinery OJSC", "FX-1008"),
        ("C.19", "A.B.C. Petrochemical Trading", "FX-1004"),
        ("C.19", "A-B-C Petrochemical Trading Company", "FX-1004"),
        ("C.21", "Ros navtek", "FX-1002"),
        ("C.21", "ZarkhovIndustrial Holdings", "FX-1001"),
        ("C.21", "ROSNAVTEKOIL", "FX-1002"),
        ("C.22", "Korniloff Vladimir", "FX-0002"),
        ("C.22", "Mokhammed Khalid Al-Rashid", "FX-0001"),
    ],
)
def test_name_evasion_variants(screen, scenario: str, name: str, entry: str) -> None:  # type: ignore[no-untyped-def]
    assert_hit(screen(bene(name)), entry)


@pytest.mark.scenario("C.11")
@pytest.mark.parametrize(
    "name,entry",
    [
        ("Владимир Сергеевич Корнилов", "FX-0002"),
        ("Владимир Корнилов", "FX-0002"),
        ("Евгений Драгунов", "FX-0003"),
        ("محمد خالد الرشيد", "FX-0001"),
        ("محمد الرشيد", "FX-0001"),
        ("李强明", "FX-0004"),
    ],
)
def test_foreign_script_input_mx(screen, name: str, entry: str) -> None:  # type: ignore[no-untyped-def]
    # MX allows UTF-8 names (MT does not), so script tests go through pacs.008.
    assert_hit(screen(pacs008(creditor=PartySpec(name=name))), entry)


@pytest.mark.scenario("C.14")
def test_name_truncated_at_35_chars(screen) -> None:  # type: ignore[no-untyped-def]
    truncated = "GOLDEN CRESCENT GENERAL TRADING FZE"[:34]
    r = screen(bene(truncated))
    assert_hit(r, "FX-1003")


@pytest.mark.scenario("C.15")
def test_name_split_across_lines(screen) -> None:  # type: ignore[no-untyped-def]
    raw = "/GB82WEST12345698765432\nVLADIMIR SERGEYEVICH\nKORNILOV\nLONDON"
    assert_hit(screen(mt103(raw_beneficiary=raw)), "FX-0002")
    # split mid-word at the 35 character line limit
    raw2 = "/GB82WEST12345698765432\nGOLDEN CRESCENT GENERAL TRADING F\nZE\nDUBAI"
    assert_hit(screen(mt103(raw_beneficiary=raw2)), "FX-1003")


@pytest.mark.scenario("C.16")
def test_name_in_field_70(screen) -> None:  # type: ignore[no-untyped-def]
    r = screen(mt103(remittance="PAYMENT INV 3321 KAVKAZ PRECISION\nMACHINERY SPARE PARTS"))
    assert_hit(r, "FX-1008")
    assert any(h.field.startswith("MT:70") for h in r.hits)


@pytest.mark.scenario("C.17")
@pytest.mark.parametrize(
    "text",
    [
        "/BNF/OBO ZARKHOV INDUSTRIAL HOLDINGS",
        "/REC/FFC ROSNAVTEK EXPORT",
        "/ACC/FOR FURTHER CREDIT TO\n//NATALIA VORONTSOVA",
    ],
)
def test_hidden_party_in_field_72(screen, text: str) -> None:  # type: ignore[no-untyped-def]
    r = screen(mt103(field72=text))
    assert r.decision != Decision.PASS
    assert hit_ids(r) & {"FX-1001", "FX-1002", "FX-0010"}
    assert any(h.field.startswith("MT:72") for h in r.hits)


@pytest.mark.scenario("C.18")
def test_name_in_address_line(screen) -> None:  # type: ignore[no-untyped-def]
    raw = "/GB82WEST12345698765432\nHARBOUR SUPPLIES LTD\nC/O ROSNAVTEK EXPORT\nLONDON"
    r = screen(mt103(raw_beneficiary=raw))
    assert_hit(r, "FX-1002")


@pytest.mark.scenario("C.20")
@pytest.mark.parametrize(
    "name",
    [
        "Vlаdimir Sergeyevich Kоrnilоv",  # Cyrillic а/о inside Latin
        "Vla\u200bdimir Ser\u200dgeyevich Kor\u200bnilov",
        "Rоsnаvtеk",
    ],
)
def test_homoglyphs_and_zero_width_mx(screen, name: str) -> None:  # type: ignore[no-untyped-def]
    assert screen(pacs008(creditor=PartySpec(name=name))).decision != Decision.PASS


@pytest.mark.scenario("C.24")
def test_alias_acronym(screen) -> None:  # type: ignore[no-untyped-def]
    assert_hit(screen(bene("ZIH GROUP")), "FX-1001")


@pytest.mark.parametrize(
    "name",
    [
        "John Smith",
        "Mohammed Ali",
        "Maria Garcia",
        "Li Wei",
        "Kim Min-jun",
        "Vladimir Petrov",
        "Golden Gate Trading Ltd",
        "ABC Logistics",
        "Hussein Ahmed",
        "Natalia Ivanova",
    ],
)
def test_common_names_do_not_hit(screen, name: str) -> None:  # type: ignore[no-untyped-def]
    r = screen(bene(name))
    assert r.decision == Decision.PASS, [h.explanation for h in r.hits]


# ------------------------------------------------------------------ D. identifiers / geo
@pytest.mark.scenario("D.1")
def test_listed_bic_blocks(screen) -> None:  # type: ignore[no-untyped-def]
    r = screen(mt202(beneficiary_institution="ZYXBIRTH"))
    assert_hit(r, "FX-1005", Decision.BLOCK)
    assert "HIT_IDENTIFIER" in r.reason_codes


@pytest.mark.scenario("D.2")
def test_branch_of_listed_bic(screen) -> None:  # type: ignore[no-untyped-def]
    r = screen(mt202(beneficiary_institution="DEUTDEFFXXX", intermediary="SNCTRUMM123"))
    assert_hit(r, "FX-1006", Decision.BLOCK)


@pytest.mark.scenario("D.3")
def test_iban_country_embargoed(screen) -> None:  # type: ignore[no-untyped-def]
    raw = "/IR062960000000100324200001\nHARBOUR SUPPLIES\nMAIN ROAD\nCITY"
    r = screen(mt103(raw_beneficiary=raw))
    assert r.decision == Decision.BLOCK and "EMBARGO_COUNTRY" in r.reason_codes


@pytest.mark.scenario("D.4")
def test_address_country_embargoed_mx(screen) -> None:  # type: ignore[no-untyped-def]
    r = screen(pacs008(creditor=PartySpec(name="Harbour Supplies", country="KP", town="NAMPO")))
    assert "EMBARGO_COUNTRY" in r.reason_codes and r.decision == Decision.BLOCK


@pytest.mark.scenario("D.5")
def test_bic_country_embargoed(screen) -> None:  # type: ignore[no-untyped-def]
    r = screen(mt202(beneficiary_institution="ABCDCUHHXXX"))
    assert "EMBARGO_COUNTRY" in r.reason_codes


@pytest.mark.scenario("D.6")
@pytest.mark.parametrize("town", ["SEVASTOPOL", "SIMFEROPOL", "LUGANSK", "Sevastopoll"])
def test_sanctioned_region(screen, town: str) -> None:  # type: ignore[no-untyped-def]
    r = screen(pacs008(creditor=PartySpec(name="Harbour Supplies", country="UA", town=town)))
    assert "EMBARGO_REGION" in r.reason_codes and r.decision == Decision.REVIEW


@pytest.mark.scenario("D.7")
def test_invalid_iban_still_screened(screen) -> None:  # type: ignore[no-untyped-def]
    raw = "/GB00WEST12345698765432\nVLADIMIR KORNILOV\nLONDON"
    r = screen(mt103(raw_beneficiary=raw))
    assert_hit(r, "FX-0002")
    assert any(i.code == "IBAN_CHECKSUM" for i in r.parse_issues)


@pytest.mark.scenario("D.8")
def test_lei_and_passport_exact(service: ScreeningService, tenant: TenantConfig) -> None:
    r = service.screen_entity(
        EntityRequest(
            name="Unrelated Name", identifiers=[{"type": "LEI", "value": "5493001KJTIIGC8Y1R12"}]
        ),
        tenant,
    )
    assert_hit(r, "FX-1008", Decision.BLOCK)
    r2 = service.screen_entity(
        EntityRequest(name="Someone", identifiers=[{"type": "PASSPORT", "value": "p 773-1942"}]),
        tenant,
    )
    assert_hit(r2, "FX-0002", Decision.BLOCK)


@pytest.mark.scenario("D.10")
def test_vessel_imo_in_free_text(screen) -> None:  # type: ignore[no-untyped-def]
    r = screen(mt103(remittance="FREIGHT MV OCEAN PEARL IMO 9123453"))
    assert_hit(r, "FX-2001", Decision.BLOCK)


@pytest.mark.scenario("D.11")
def test_country_in_free_text(screen) -> None:  # type: ignore[no-untyped-def]
    r = screen(mt103(remittance="GOODS TO BE SHIPPED TO IRAN VIA JEBEL ALI"))
    assert "EMBARGO_COUNTRY" in r.reason_codes


@pytest.mark.scenario("A.11")
def test_77b_residence_code(screen) -> None:  # type: ignore[no-untyped-def]
    r = screen(mt103(extra_fields=[("77B", "/BENEFRES/IR//")]))
    assert "EMBARGO_COUNTRY" in r.reason_codes


@pytest.mark.scenario("D.12")
def test_no_geo_false_positive_on_codes_in_words(screen) -> None:  # type: ignore[no-untyped-def]
    r = screen(mt103(remittance="FIRST INSTALMENT IRREVOCABLE KPI BONUS CUBAGE"))
    assert r.decision == Decision.PASS, r.geo_findings


# ------------------------------------------------------------------ COV and MX parties
@pytest.mark.scenario("A.8")
def test_mt202cov_underlying_beneficiary_hit(screen) -> None:  # type: ignore[no-untyped-def]
    r = screen(
        mt202cov(
            ordering=PartySpec(name="HARBOUR SUPPLIES"),
            beneficiary=PartySpec(name="NATALIA IGOREVNA VORONTSOVA"),
        )
    )
    assert_hit(r, "FX-0010")
    assert any(h.field.startswith("MT:B/59") for h in r.hits)


@pytest.mark.scenario("B.11")
def test_pacs009_cov_underlying_hit(screen) -> None:  # type: ignore[no-untyped-def]
    r = screen(pacs009(cov_creditor=PartySpec(name="Kim Chol Song")))
    assert_hit(r, "FX-0007")
    assert any("UndrlygCstmrCdtTrf" in h.field for h in r.hits)


@pytest.mark.scenario("B.3")
def test_ultimate_creditor_hit(screen) -> None:  # type: ignore[no-untyped-def]
    assert_hit(
        screen(pacs008(ultimate_creditor=PartySpec(name="Osama Faris Bin Talal"))), "FX-0009"
    )


@pytest.mark.scenario("B.2")
def test_worst_transaction_rolls_up(screen) -> None:  # type: ignore[no-untyped-def]
    xml = pacs008(transactions=3, seed=3).replace(
        "</CdtTrfTxInf></FIToFICstmrCdtTrf>",
        "<RmtInf><Ustrd>for Rosnavtek</Ustrd></RmtInf></CdtTrfTxInf></FIToFICstmrCdtTrf>",
    )
    r = screen(xml)
    assert_hit(r, "FX-1002")
    assert any("CdtTrfTxInf[2]" in h.field for h in r.hits)


def test_mx_remittance_hidden_party(screen) -> None:  # type: ignore[no-untyped-def]
    r = screen(pacs008(remittance="Invoice 22 on behalf of Al Noor Exchange"))
    assert_hit(r, "FX-1009")


# ------------------------------------------------------------------ lists: PEP / internal
@pytest.mark.scenario("E.4")
def test_pep_hit_is_review(screen) -> None:  # type: ignore[no-untyped-def]
    r = screen(bene("Maria Teresa Valcourt"))
    assert r.decision == Decision.REVIEW and r.reason_codes == ["HIT_PEP"]


def test_internal_list_hit(screen) -> None:  # type: ignore[no-untyped-def]
    r = screen(bene("Dmitri Olegovich Sablin"))
    assert r.decision == Decision.REVIEW and "HIT_INTERNAL" in r.reason_codes


# ------------------------------------------------------------------ fail closed
@pytest.mark.scenario("A.12")
def test_parse_error_with_hit_keeps_hit_and_reviews(screen) -> None:  # type: ignore[no-untyped-def]
    msg = mt103(beneficiary=PartySpec(name="KIM CHOL SONG")).replace(":71A:SHA\n", "")
    r = screen(msg)
    assert "PARSE_FAILED" in r.reason_codes and "FX-0007" in hit_ids(r)
    assert r.decision == Decision.BLOCK


@pytest.mark.parametrize(
    "raw",
    [
        "garbage that is not swift at all",
        "{1:F01BANKGB2LAXXX0000000000}{4:\n:20:X\n:59:KIM CHOL SONG",
    ],
)
def test_unparseable_input_is_review_and_still_screened(screen, raw: str) -> None:  # type: ignore[no-untyped-def]
    r = screen(raw)
    assert r.decision != Decision.PASS and "PARSE_FAILED" in r.reason_codes
    if "KIM CHOL SONG" in raw:
        assert "FX-0007" in hit_ids(r)


def test_xml_attack_is_review_not_crash(screen) -> None:  # type: ignore[no-untyped-def]
    r = screen('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///c:/x">]><d>&x;</d>')
    assert r.decision == Decision.REVIEW and "PARSE_FAILED" in r.reason_codes


@pytest.mark.scenario("L.6")
def test_stale_list_holds(tmp_path, config) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime, timedelta

    from tests.conftest import load_fixture_lists

    store = ListStore(tmp_path / "lists")
    load_fixture_lists(store)
    st = store.state("FIXTURE_SANCTIONS")
    st.last_success_at = datetime.now(UTC) - timedelta(hours=100)
    store._write_state(st)
    svc = ScreeningService(store, config)
    t = config.tenant("t1")
    r = svc.screen_mt(mt103(seed=1), t)
    assert r.decision == Decision.REVIEW and "LIST_STALE" in r.reason_codes
    assert r.stale_lists == ["FIXTURE_SANCTIONS"]
    flag = t.model_copy(update={"lists": t.lists.model_copy(update={"stale_action": "flag"})})
    r2 = svc.screen_mt(mt103(seed=1), flag)
    assert r2.decision == Decision.PASS and "LIST_STALE" in r2.reason_codes


def test_no_lists_loaded_fails_closed(tmp_path, config) -> None:  # type: ignore[no-untyped-def]
    svc = ScreeningService(ListStore(tmp_path / "empty"), config)
    r = svc.screen_mt(mt103(seed=1), config.tenant("t1"))
    assert r.decision == Decision.REVIEW and "LIST_UNAVAILABLE" in r.reason_codes


def test_required_list_missing_fails_closed(tmp_path, config) -> None:  # type: ignore[no-untyped-def]
    from swift_screening.lists.model import ListType
    from swift_screening.lists.registry import connector_for, refresh_list
    from tests.conftest import FIXTURES

    store = ListStore(tmp_path / "lists")
    refresh_list(
        store,
        connector_for("json", "FIXTURE_PEP", ListType.PEP),
        str(FIXTURES / "fixture_pep.json"),
    )
    r = ScreeningService(store, config).screen_mt(mt103(seed=1), config.tenant("t1"))
    assert r.decision == Decision.REVIEW and "LIST_UNAVAILABLE" in r.reason_codes


def test_internal_error_fails_closed(
    service: ScreeningService, tenant: TenantConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import swift_screening.service as svc_mod

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("matcher exploded")

    monkeypatch.setattr(svc_mod.Matcher, "screen", boom)
    r = service.screen_mt(bene("John Smith"), tenant)
    assert r.decision == Decision.REVIEW and r.reason_codes == ["INTERNAL_ERROR"]


@pytest.mark.scenario("A.17")
def test_possible_duplicate_reason(screen) -> None:  # type: ignore[no-untyped-def]
    r = screen(mt103(seed=2).replace("{5:{CHK:123456789ABC}}", "{5:{CHK:1}{PDE:}}"))
    assert "POSSIBLE_DUPLICATE" in r.reason_codes


def test_public_mode_buckets_scores(service: ScreeningService, config) -> None:  # type: ignore[no-untyped-def]
    r = service.screen_mt(bene("Kim Chol Song"), config.tenant("pub"))
    assert r.decision == Decision.BLOCK and r.risk_score is None
    assert all(h.score is None and h.input_value is None for h in r.hits)
