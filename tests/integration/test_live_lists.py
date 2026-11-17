"""Opt-in: validate connectors against the live official files.

Run with:  SWIFT_SCREENING_LIVE_LISTS=1 uv run pytest tests/integration/test_live_lists.py -s
Downloads tens of MB; skipped by default (and in CI) because sources may be slow or change.
"""

from __future__ import annotations

import os
import re

import pytest

from swift_screening.lists.connectors.base import fetch_source
from swift_screening.lists.connectors.official import (
    EuFsfConnector,
    OfacSdnConnector,
    UnConsolidatedConnector,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("SWIFT_SCREENING_LIVE_LISTS") != "1", reason="live list check is opt-in"
)


def test_ofac_live_matches_record_count() -> None:
    c = OfacSdnConnector()
    assert c.default_url
    data = fetch_source(c.default_url, timeout=600)
    snap = c.snapshot(data, c.default_url)
    m = re.search(rb"<Record_Count>(\d+)</Record_Count>", data[:5000])
    assert m and len(snap.entries) == int(m.group(1))
    assert sum(e.entry_type == "vessel" for e in snap.entries) > 100
    # attributes must not leak into identifiers
    assert not any(
        i.value.lower() in {"male", "female"} for e in snap.entries for i in e.identifiers
    )


def test_un_live() -> None:
    c = UnConsolidatedConnector()
    assert c.default_url
    data = fetch_source(c.default_url, timeout=600)
    snap = c.snapshot(data, c.default_url)
    assert len(snap.entries) == data.count(b"<INDIVIDUAL>") + data.count(b"<ENTITY>")
    assert len(snap.entries) > 500


def test_eu_live() -> None:
    c = EuFsfConnector()
    url = c.default_url
    assert url
    data = fetch_source(url, timeout=900)
    snap = c.snapshot(data, url)
    assert len(snap.entries) == data.count(b"<sanctionEntity ")
    assert len(snap.entries) > 3000


# Must PASS on the live lists: no listed person or alias resembles these.
STRICT_PASS = [
    "John Smith",
    "David Miller",
    "Anna Kowalska",
    "Hans Schmidt",
    "Michael Brown",
    "Sarah Johnson",
    "Andrea Rossi",
    "Jan Kowalski",
]
# May REVIEW (they resemble real list aliases, e.g. "Juan Perez" = first + last of
# "Juan Manual MEDINA PEREZ"), but must never auto-BLOCK.
NEVER_BLOCK = [
    "Maria Garcia",
    "Juan Perez",
    "Mohammed Ahmed",
    "Ali Hassan",
    "Abdul Rahman",
    "Global Trading Ltd",
]
COMMON_NAMES = STRICT_PASS + NEVER_BLOCK


def test_live_lists_common_names_never_block(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Common names may legitimately REVIEW (real aliases exist) but must never auto-BLOCK,
    and western common names with no listed alias must PASS."""
    from swift_screening.config.models import AppConfig, ListSettings
    from swift_screening.lists.registry import connector_for, refresh_list
    from swift_screening.lists.store import ListStore
    from swift_screening.service import ScreeningService
    from swift_screening.synth.generator import PartySpec, mt103
    from tests.conftest import make_config

    store = ListStore(tmp_path)
    for fmt in ("ofac", "un"):
        refresh_list(store, connector_for(fmt))
    base = make_config().tenant("t1")
    assert base is not None
    t = base.model_copy(update={"lists": ListSettings(enabled=["OFAC_SDN", "UN_CONSOLIDATED"])})
    svc = ScreeningService(store, AppConfig(tenants=[t]))
    for i, name in enumerate(COMMON_NAMES):
        r = svc.screen_mt(mt103(beneficiary=PartySpec(name=name), seed=i), t)
        assert r.decision != "BLOCK", (name, [h.explanation for h in r.hits])
        if name in STRICT_PASS:
            assert r.decision == "PASS", (name, [h.explanation for h in r.hits])
