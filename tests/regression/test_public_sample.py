"""Recall / false positives on 200 real OFAC SDN entries (public domain), offline.

The fictional fixtures are too clean: validating against live lists found false positives they
could never show (see docs/adr/0001). This sample keeps real-world name shapes in every CI run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swift_screening.config.models import AppConfig, ListSettings, TenantConfig
from swift_screening.decision.models import Decision
from swift_screening.lists.model import ListType
from swift_screening.lists.registry import connector_for, refresh_list
from swift_screening.lists.store import ListStore
from swift_screening.service import ScreeningService
from swift_screening.synth.benchmark import run_benchmark
from swift_screening.synth.generator import PartySpec, mt103
from tests.conftest import FIXTURES, make_config

COMMON_NAMES = [
    "John Smith",
    "Maria Garcia",
    "David Miller",
    "Anna Kowalska",
    "Hans Schmidt",
    "Juan Perez",
    "Michael Brown",
    "Sarah Johnson",
    "Andrea Rossi",
    "Jan Kowalski",
    "Pierre Martin",
    "Olga Ivanova",
    "Kenji Tanaka",
    "Priya Sharma",
    "Li Wei",
]


@pytest.fixture(scope="module")
def public(tmp_path_factory: pytest.TempPathFactory) -> tuple[ScreeningService, TenantConfig]:
    store = ListStore(tmp_path_factory.mktemp("public"))
    refresh_list(
        store,
        connector_for("json", "PUBLIC_OFAC_SAMPLE", ListType.SANCTIONS),
        str(FIXTURES / "public_ofac_sample.json"),
    )
    base = make_config().tenant("t1")
    assert base is not None
    t = base.model_copy(update={"lists": ListSettings(enabled=["PUBLIC_OFAC_SAMPLE"])})
    return ScreeningService(store, AppConfig(tenants=[t])), t


def test_fixture_has_provenance() -> None:
    import json

    doc = json.loads(Path(FIXTURES / "public_ofac_sample.json").read_text(encoding="utf-8"))
    assert "public domain" in doc["_licence"] and "OFAC" in doc["_source"]
    assert len(doc["entries"]) >= 200


def test_public_sample_recall_and_false_positives(public) -> None:  # type: ignore[no-untyped-def]
    svc, t = public
    res = run_benchmark(
        svc, t, negatives=200, primary_only=True, fields={"MT:59", "MX:Cdtr", "MX:Ustrd", "MT:70"}
    )
    print(res.report())
    assert res.total > 2000
    assert res.recall == 1.0, res.report()
    assert res.fp_rate <= 0.01, res.report()
    assert res.hard_fp_rate <= 0.05, res.report()


def test_common_names_against_public_sample(public) -> None:  # type: ignore[no-untyped-def]
    svc, t = public
    for i, name in enumerate(COMMON_NAMES):
        r = svc.screen_mt(mt103(beneficiary=PartySpec(name=name), seed=i), t)
        assert r.decision == Decision.PASS, (name, [h.explanation for h in r.hits])
