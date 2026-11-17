"""Phase 1 performance gate: p95 < 150 ms for a single MT103 against 50k list entries."""

from __future__ import annotations

import random
import statistics
import time
from pathlib import Path

import pytest

from swift_screening.config.models import AppConfig
from swift_screening.lists.connectors.internal import JsonListConnector
from swift_screening.lists.model import ListType, WatchlistEntry, content_hash
from swift_screening.lists.store import ListStore
from swift_screening.service import ScreeningService
from swift_screening.synth.generator import PartySpec, fake_company, fake_name, mt103, pacs008
from tests.conftest import load_fixture_lists

N_ENTRIES = 50_000
P95_BUDGET_MS = 150.0


@pytest.fixture(scope="module")
def big_service(tmp_path_factory: pytest.TempPathFactory, config: AppConfig) -> ScreeningService:
    rng = random.Random(42)
    entries = []
    for i in range(N_ENTRIES):
        org = i % 3 == 0
        name = fake_company(rng) if org else fake_name(rng, rng.choice([2, 3, 4]))
        aliases = [fake_name(rng, 2)] if i % 4 == 0 else []
        entries.append(
            WatchlistEntry(
                list_id="FIXTURE_PEP",
                entry_id=f"S{i}",
                name=name,
                aliases=aliases,
                list_type=ListType.PEP,
                entry_type="organisation" if org else "individual",
            )
        )
    store = ListStore(tmp_path_factory.mktemp("big"))
    load_fixture_lists(store)
    conn = JsonListConnector("FIXTURE_PEP", ListType.PEP)
    from swift_screening.lists.model import ListSnapshot

    store.save(
        ListSnapshot(
            list_id="FIXTURE_PEP",
            list_type=ListType.PEP,
            source="synthetic",
            content_hash=content_hash(entries),
            entries=entries,
        ),
        force=True,
    )
    del conn
    svc = ScreeningService(store, config)
    t0 = time.perf_counter()
    svc.index_for(svc.resolve_versions(config.tenants[0])[0])
    print(f"index build for {N_ENTRIES + 20} entries: {time.perf_counter() - t0:.1f}s")
    return svc


def _p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=20)[18]


@pytest.mark.perf
def test_mt103_p95_latency(big_service: ScreeningService, config: AppConfig) -> None:
    tenant = config.tenants[0]
    rng = random.Random(1)
    msgs = [
        mt103(
            seed=i,
            beneficiary=PartySpec(name=fake_name(rng, 3)),
            remittance=f"INVOICE {i} FOR {fake_company(rng).upper()} GOODS",
        )
        for i in range(200)
    ]
    for m in msgs[:10]:  # warm-up
        big_service.screen_mt(m, tenant)
    samples = []
    for m in msgs:
        t0 = time.perf_counter()
        big_service.screen_mt(m, tenant)
        samples.append((time.perf_counter() - t0) * 1000)
    p95 = _p95(samples)
    print(
        f"MT103 latency ms: p50={statistics.median(samples):.1f} p95={p95:.1f} "
        f"max={max(samples):.1f}"
    )
    assert p95 < P95_BUDGET_MS


@pytest.mark.perf
def test_pacs008_p95_latency(big_service: ScreeningService, config: AppConfig) -> None:
    tenant = config.tenants[0]
    rng = random.Random(2)
    samples = []
    for i in range(100):
        msg = pacs008(
            seed=i,
            creditor=PartySpec(name=fake_name(rng, 3)),
            remittance=f"Invoice {i} {fake_company(rng)}",
        )
        t0 = time.perf_counter()
        big_service.screen_mx(msg, tenant)
        samples.append((time.perf_counter() - t0) * 1000)
    p95 = _p95(samples)
    print(f"pacs.008 latency ms: p50={statistics.median(samples):.1f} p95={p95:.1f}")
    assert p95 < P95_BUDGET_MS


@pytest.mark.perf
def test_recall_holds_at_scale(big_service: ScreeningService, config: AppConfig) -> None:
    """Large list must not crowd out true hits (candidate caps)."""
    tenant = config.tenants[0]
    for name, entry in [
        ("MUHAMMAD KHALED AL RASHEED", "FX-0001"),
        ("KORNILOFF VLADIMIR", "FX-0002"),
        ("ROSNAVTEKOIL", "FX-1002"),
    ]:
        r = big_service.screen_mt(mt103(beneficiary=PartySpec(name=name)), tenant)
        assert entry in {h.matched_entry.id for h in r.hits}, name


def test_store_round_trip(tmp_path: Path) -> None:
    # keeps this module importable/collectable when perf tests are deselected
    assert ListStore(tmp_path).list_ids() == []
