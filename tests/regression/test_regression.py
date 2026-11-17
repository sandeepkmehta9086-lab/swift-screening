"""Frozen regression gate: 100% recall on tests/regression/cases.yaml, plus the recall benchmark.

If this fails, fix the change - never the test (Agent.md section 0, rule 4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from swift_screening.config.models import TenantConfig
from swift_screening.decision.models import Decision, ScreeningResult
from swift_screening.service import EntityRequest, ScreeningService
from swift_screening.synth.benchmark import run_benchmark
from swift_screening.synth.generator import PartySpec, mt103, mt202, mt202cov, pacs008, pacs009

CASES_FILE = Path(__file__).with_name("cases.yaml")
CASES: list[dict[str, Any]] = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))["cases"]
MIN_CASES = 52  # the suite may only grow


def run_case(
    service: ScreeningService, tenant: TenantConfig, case: dict[str, Any]
) -> ScreeningResult:
    v: str = case["value"]
    carrier = case["carrier"]
    mt_builders = {
        "mt103_59": lambda: mt103(beneficiary=PartySpec(name=v)),
        "mt103_50k": lambda: mt103(ordering=PartySpec(name=v)),
        "mt103_59_raw": lambda: mt103(raw_beneficiary=v),
        "mt103_70": lambda: mt103(remittance=v),
        "mt103_72": lambda: mt103(field72=v),
        "mt202_58a": lambda: mt202(beneficiary_institution=v),
        "mt202_56a": lambda: mt202(intermediary=v),
        "mt202cov_b59": lambda: mt202cov(
            ordering=PartySpec(name="HARBOUR SUPPLIES"), beneficiary=PartySpec(name=v)
        ),
    }
    mx_builders = {
        "pacs008_cdtr": lambda: pacs008(creditor=PartySpec(name=v)),
        "pacs008_ultmtcdtr": lambda: pacs008(ultimate_creditor=PartySpec(name=v)),
        "pacs008_ustrd": lambda: pacs008(remittance=v),
        "pacs009_cov_cdtr": lambda: pacs009(cov_creditor=PartySpec(name=v)),
    }
    if carrier in mt_builders:
        return service.screen_mt(mt_builders[carrier](), tenant)
    if carrier in mx_builders:
        return service.screen_mx(mx_builders[carrier](), tenant)
    if carrier == "entity":
        return service.screen_entity(EntityRequest(name=v), tenant)
    if carrier in {"entity_passport", "entity_lei"}:
        id_type = "PASSPORT" if carrier == "entity_passport" else "LEI"
        return service.screen_entity(
            EntityRequest(name="Unrelated Name", identifiers=[{"type": id_type, "value": v}]),
            tenant,
        )
    raise AssertionError(f"unknown carrier {carrier}")


def test_suite_only_grows() -> None:
    assert len(CASES) >= MIN_CASES, "regression cases were removed"
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)), "duplicate regression case ids"


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_regression_case(
    service: ScreeningService, tenant: TenantConfig, case: dict[str, Any]
) -> None:
    r = run_case(service, tenant, case)
    min_decision = Decision(case["min_decision"])
    hit = [h for h in r.hits if h.matched_entry.id == case["expect_entry"]]
    assert hit, (
        f"{case['id']} ({case['scenario']}): no hit on {case['expect_entry']}; "
        f"got {[(h.matched_entry.id, h.score) for h in r.hits]}"
    )
    assert r.decision.severity >= min_decision.severity, (
        f"{case['id']}: decision {r.decision} < {min_decision}"
    )


def test_recall_benchmark(service: ScreeningService, tenant: TenantConfig) -> None:
    """Every list name x 17 obfuscations x 8 fields; plus random and hard negatives."""
    res = run_benchmark(service, tenant, negatives=300)
    print(res.report())
    assert res.total > 2500
    assert res.recall == 1.0, res.report()
    assert res.fp_rate <= 0.01, res.report()
    assert res.hard_fp_rate <= 0.05, res.report()
