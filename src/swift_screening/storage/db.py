"""Persistence: screening results + hash-chained append-only audit log (SQLAlchemy 2.x).

Works on PostgreSQL (production) and SQLite (tests / local demo). Raw message bodies are never
stored. In stateless mode the stored result is redacted (no input values) as well.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

GENESIS_HASH = "0" * 64


class Base(DeclarativeBase):
    pass


class ResultRow(Base):
    __tablename__ = "screening_results"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_tenant_idem"),)

    screening_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    decision: Mapped[str] = mapped_column(String(16))
    result_json: Mapped[str] = mapped_column(Text)
    redacted: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AuditRow(Base):
    __tablename__ = "audit_log"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    ref_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[str] = mapped_column(Text)
    prev_hash: Mapped[str] = mapped_column(String(64))
    row_hash: Mapped[str] = mapped_column(String(64), unique=True)


class AuditHead(Base):
    __tablename__ = "audit_head"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_hash: Mapped[str] = mapped_column(String(64))
    last_seq: Mapped[int] = mapped_column(Integer)


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def chain_hash(prev_hash: str, payload_json: str) -> str:
    return hashlib.sha256(f"{prev_hash}|{payload_json}".encode()).hexdigest()


class StorageUnavailable(Exception):
    """The database cannot be reached; the API must answer 503, never PASS."""


class Database:
    def __init__(self, url: str) -> None:
        kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.engine: Engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def _pragmas(dbapi_conn: Any, _: Any) -> None:
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()

        self.is_postgres = self.engine.dialect.name == "postgresql"
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        self._lock = threading.Lock()

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def ping(self) -> bool:
        try:
            with self.engine.connect() as c:
                c.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    @contextmanager
    def session(self) -> Iterator[Session]:
        try:
            with self.Session.begin() as s:
                yield s
        except Exception as exc:
            from sqlalchemy.exc import OperationalError

            if isinstance(exc, OperationalError):
                raise StorageUnavailable(str(exc)) from exc
            raise

    # ------------------------------------------------------------------ results
    def find_by_idempotency(self, tenant_id: str, key: str) -> str | None:
        with self.session() as s:
            row = s.scalar(
                select(ResultRow).where(
                    ResultRow.tenant_id == tenant_id, ResultRow.idempotency_key == key
                )
            )
            return row.result_json if row else None

    def get_result(self, tenant_id: str, screening_id: str) -> str | None:
        with self.session() as s:
            row = s.get(ResultRow, screening_id)
            if row is None or row.tenant_id != tenant_id:
                return None  # never reveal another tenant's result exists
            return row.result_json

    def save_result_with_audit(
        self,
        *,
        screening_id: str,
        tenant_id: str,
        idempotency_key: str,
        input_hash: str,
        decision: str,
        result_json: str,
        redacted: bool,
        audit_payload: dict[str, Any],
        created_at: datetime,
    ) -> bool:
        """Persist result and its audit row atomically. Returns False on idempotency conflict."""
        from sqlalchemy.exc import IntegrityError

        with self._lock:
            try:
                with self.session() as s:
                    s.add(
                        ResultRow(
                            screening_id=screening_id,
                            tenant_id=tenant_id,
                            idempotency_key=idempotency_key,
                            input_hash=input_hash,
                            decision=decision,
                            result_json=result_json,
                            redacted=int(redacted),
                            created_at=created_at,
                        )
                    )
                    s.flush()
                    self._append_audit(
                        s, tenant_id, "screening", screening_id, audit_payload, created_at
                    )
                return True
            except IntegrityError:
                return False

    # ------------------------------------------------------------------ audit
    def _append_audit(
        self,
        s: Session,
        tenant_id: str,
        event_type: str,
        ref_id: str | None,
        payload: dict[str, Any],
        created_at: datetime,
    ) -> AuditRow:
        stmt = select(AuditHead).where(AuditHead.id == 1)
        if self.is_postgres:
            stmt = stmt.with_for_update()
        head = s.scalar(stmt)
        if head is None:
            head = AuditHead(id=1, last_hash=GENESIS_HASH, last_seq=0)
            s.add(head)
            s.flush()
        body = canonical_json(
            {
                "tenant_id": tenant_id,
                "event_type": event_type,
                "ref_id": ref_id,
                "created_at": created_at.isoformat(),
                "payload": payload,
            }
        )
        row = AuditRow(
            tenant_id=tenant_id,
            event_type=event_type,
            ref_id=ref_id,
            created_at=created_at,
            payload_json=body,
            prev_hash=head.last_hash,
            row_hash=chain_hash(head.last_hash, body),
        )
        s.add(row)
        s.flush()
        head.last_hash = row.row_hash
        head.last_seq = row.seq
        return row

    def append_audit(
        self, tenant_id: str, event_type: str, ref_id: str | None, payload: dict[str, Any]
    ) -> None:
        with self._lock, self.session() as s:
            self._append_audit(s, tenant_id, event_type, ref_id, payload, datetime.now(UTC))

    def verify_chain(self) -> tuple[bool, int | None, int]:
        """Recompute the chain. Returns (ok, first_bad_seq, rows_checked)."""
        prev = GENESIS_HASH
        checked = 0
        with self.session() as s:
            for row in s.scalars(select(AuditRow).order_by(AuditRow.seq)):
                checked += 1
                if row.prev_hash != prev or chain_hash(prev, row.payload_json) != row.row_hash:
                    return False, row.seq, checked
                prev = row.row_hash
            head = s.scalar(select(AuditHead).where(AuditHead.id == 1))
            if head is not None and head.last_hash != prev:
                return False, head.last_seq, checked  # tail truncation
        return True, None, checked

    # ------------------------------------------------------------------ retention
    def purge_expired(self, retention_days: int, now: datetime | None = None) -> int:
        """Redact input values of results older than the retention period; keep decisions."""
        from swift_screening.storage.redact import redact_result_json

        cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
        n = 0
        with self.session() as s:
            rows = s.scalars(select(ResultRow).where(ResultRow.redacted == 0))
            for row in rows:
                created = (
                    row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
                )
                if created < cutoff:
                    row.result_json = redact_result_json(row.result_json)
                    row.redacted = 1
                    n += 1
        return n
