"""Audit log and data protection I.1 - I.6."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
import structlog
from sqlalchemy import select, update

from swift_screening.config.models import AppConfig
from swift_screening.lists.model import ListType
from swift_screening.lists.registry import connector_for, refresh_list
from swift_screening.service import ScreeningService
from swift_screening.storage.db import AuditRow, Database, ResultRow
from swift_screening.synth.generator import PartySpec, mt103


@pytest.mark.scenario("I.1")
def test_every_screening_writes_audit_row(
    service_db: ScreeningService, config: AppConfig, db: Database
) -> None:
    t = config.tenant("t1")
    results = [service_db.screen_mt(mt103(seed=i), t) for i in range(5)]
    results.append(service_db.screen_mt(mt103(beneficiary=PartySpec(name="KIM CHOL SONG")), t))
    with db.session() as s:
        rows = list(s.scalars(select(AuditRow).order_by(AuditRow.seq)))
    assert [r.ref_id for r in rows] == [r.screening_id for r in results]
    payload = json.loads(rows[-1].payload_json)["payload"]
    assert payload["decision"] == "BLOCK"
    assert payload["hits"] == ["FIXTURE_SANCTIONS:FX-0007:BLOCK"]
    assert payload["list_versions"] == results[-1].list_versions
    for key in ("input_hash", "config_version", "rule_set_version", "engine_version"):
        assert payload[key]
    assert "KIM CHOL SONG" not in rows[-1].payload_json  # no PII in the audit chain


@pytest.mark.scenario("I.2")
def test_tampering_detected(service_db: ScreeningService, config: AppConfig, db: Database) -> None:
    t = config.tenant("t1")
    for i in range(10):
        service_db.screen_mt(mt103(seed=i), t)
    ok, bad, n = db.verify_chain()
    assert ok and bad is None and n == 10
    with db.session() as s:
        row = s.scalars(select(AuditRow).where(AuditRow.seq == 4)).one()
        row.payload_json = row.payload_json.replace('"PASS"', '"BLOCK"')
    ok, bad, _ = db.verify_chain()
    assert not ok and bad == 4


def test_deleted_row_and_truncation_detected(
    service_db: ScreeningService, config: AppConfig, db: Database
) -> None:
    t = config.tenant("t1")
    for i in range(6):
        service_db.screen_mt(mt103(seed=100 + i), t)
    with db.session() as s:
        s.delete(s.scalars(select(AuditRow).where(AuditRow.seq == 6)).one())  # tail truncation
    assert db.verify_chain()[0] is False


def test_middle_row_deletion_detected(
    service_db: ScreeningService, config: AppConfig, db: Database
) -> None:
    t = config.tenant("t1")
    for i in range(6):
        service_db.screen_mt(mt103(seed=200 + i), t)
    with db.session() as s:
        s.delete(s.scalars(select(AuditRow).where(AuditRow.seq == 3)).one())
    ok, bad, _ = db.verify_chain()
    assert not ok and bad == 4


@pytest.mark.scenario("I.3")
def test_stateless_mode_stores_no_message_content(
    service_db: ScreeningService, config: AppConfig, db: Database
) -> None:
    t = config.tenant("sl")
    # Misspelt on purpose: the stored result legitimately contains the *list entry* name
    # (public list data); what must not be stored is the message's own content.
    name = "NATALIA IGOREVNA VORONTSAVA"
    r = service_db.screen_mt(
        mt103(beneficiary=PartySpec(name=name), remittance="SECRET INVOICE TEXT"), t
    )
    assert r.decision == "REVIEW" and r.stateless
    assert {h.matched_entry.id for h in r.hits} == {"FX-0010"}
    assert all(h.input_value is None for h in r.hits)
    assert r.message_id is None
    with db.session() as s:
        stored = s.scalars(select(ResultRow)).one()
        audit = s.scalars(select(AuditRow)).one()
    for blob in (stored.result_json, audit.payload_json):
        assert name not in blob.upper() and "SECRET INVOICE" not in blob.upper()
        assert "GB82WEST" not in blob


@pytest.mark.scenario("I.4")
def test_logs_contain_no_pii(
    service_db: ScreeningService, config: AppConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    structlog.configure(
        processors=[structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        logger_factory=structlog.PrintLoggerFactory(),
    )
    try:
        service_db.screen_mt(
            mt103(beneficiary=PartySpec(name="KIM CHOL SONG"), remittance="PRIVATE NOTE 123"),
            config.tenant("t1"),
        )
        out = capsys.readouterr().out
        assert "screened" in out
        assert "KIM CHOL SONG" not in out and "PRIVATE NOTE" not in out
        assert "GB82WEST" not in out
    finally:
        structlog.reset_defaults()
        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))


@pytest.mark.scenario("I.5")
@pytest.mark.scenario("F.6")
def test_replay_with_recorded_versions_and_delisting(
    service_db: ScreeningService, config: AppConfig, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    t = config.tenant("t1")
    msg = mt103(beneficiary=PartySpec(name="KIM CHOL SONG"), seed=77)
    first = service_db.screen_mt(msg, t)
    assert first.decision == "BLOCK"
    # De-list FX-0007 in a new snapshot.
    from tests.conftest import FIXTURES

    data = json.loads((FIXTURES / "fixture_sanctions.json").read_text(encoding="utf-8"))
    data["entries"] = [e for e in data["entries"] if e["entry_id"] != "FX-0007"]
    data["published_at"] = "2026-11-14T18:00:00+00:00"
    newer = tmp_path / "delisted.json"
    newer.write_text(json.dumps(data), encoding="utf-8")
    refresh_list(
        service_db.store, connector_for("json", "FIXTURE_SANCTIONS", ListType.SANCTIONS), str(newer)
    )
    service_db.invalidate()
    after = service_db.screen_mt(msg, t)
    assert after.decision == "PASS"
    assert after.list_versions["FIXTURE_SANCTIONS"] != first.list_versions["FIXTURE_SANCTIONS"]
    # Replay against the versions recorded on the first result -> same decision.
    replay = service_db.screen_mt(msg, t, list_versions=first.list_versions)
    assert replay.decision == first.decision
    assert {h.matched_entry.id for h in replay.hits} == {h.matched_entry.id for h in first.hits}
    # The historic result still cites the old version.
    stored = service_db.get_result(t, first.screening_id)
    assert stored is not None and stored.list_versions == first.list_versions


@pytest.mark.scenario("I.6")
def test_retention_purge_redacts_but_keeps_decision(
    service_db: ScreeningService, config: AppConfig, db: Database
) -> None:
    t = config.tenant("t1")
    r = service_db.screen_mt(mt103(beneficiary=PartySpec(name="KIM CHOL SONG"), seed=5), t)
    with db.session() as s:
        s.execute(update(ResultRow).values(created_at=datetime.now(UTC) - timedelta(days=400)))
    assert db.purge_expired(retention_days=365) == 1
    stored = service_db.get_result(t, r.screening_id)
    assert stored is not None and stored.decision == "BLOCK"
    assert all(h.input_value is None for h in stored.hits)
    assert "KIM CHOL SONG" not in stored.model_dump_json()
    assert db.purge_expired(retention_days=365) == 0
    assert db.verify_chain()[0]  # purge never touches the audit chain


def test_concurrent_screening_keeps_chain_valid(
    service_db: ScreeningService, config: AppConfig, db: Database
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    t = config.tenant("t1")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: service_db.screen_mt(mt103(seed=1000 + i), t), range(40)))
    ok, _, n = db.verify_chain()
    assert ok and n == 40
