"""Versioned watchlist snapshot store (filesystem) with freshness tracking and shrink guard.

Layout::

    <root>/<LIST_ID>/snapshots/<version>.json.gz   every accepted snapshot (history kept)
    <root>/<LIST_ID>/state.json                    current version, last success / failure

Freshness is measured from the last *successful check of the source* (``last_success_at``), not
from the publisher's timestamp: a list that is unchanged at the source is still fresh, while a
list whose source we failed to reach for longer than the SLA is stale.
"""

from __future__ import annotations

import gzip
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from swift_screening.lists.model import ListSnapshot, SnapshotDiff, diff_snapshots


class ListShrinkError(Exception):
    """A new snapshot is suspiciously smaller than the current one (possible truncation)."""


class ListState(BaseModel):
    list_id: str
    current_version: str | None = None
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0


class ListStatus(BaseModel):
    list_id: str
    list_type: str | None
    version: str | None
    published_at: datetime | None
    last_success_at: datetime | None
    age_hours: float | None
    sla_hours: float
    stale: bool
    entries: int
    last_error: str | None


def _safe(version: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", version)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class ListStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._cache: dict[tuple[str, str], ListSnapshot] = {}

    # ------------------------------------------------------------------ state
    def _state_path(self, list_id: str) -> Path:
        return self.root / list_id / "state.json"

    def state(self, list_id: str) -> ListState:
        p = self._state_path(list_id)
        if p.is_file():
            return ListState.model_validate_json(p.read_bytes())
        return ListState(list_id=list_id)

    def _write_state(self, state: ListState) -> None:
        _atomic_write(self._state_path(state.list_id), state.model_dump_json(indent=2).encode())

    def record_failure(self, list_id: str, error: str, now: datetime | None = None) -> ListState:
        st = self.state(list_id)
        st.last_attempt_at = now or datetime.now(UTC)
        st.last_error = error[:500]
        st.consecutive_failures += 1
        self._write_state(st)
        return st

    # ------------------------------------------------------------------ snapshots
    def save(
        self,
        snapshot: ListSnapshot,
        *,
        max_shrink_ratio: float = 0.3,
        force: bool = False,
        now: datetime | None = None,
    ) -> SnapshotDiff:
        now = now or datetime.now(UTC)
        current = self.current(snapshot.list_id)
        if (
            current is not None
            and current.content_hash == snapshot.content_hash
            and current.published_at == snapshot.published_at
        ):
            # Unchanged source: keep the version (stable idempotency keys, no index rebuild,
            # no duplicate snapshot); only record the successful check for freshness.
            st = self.state(snapshot.list_id)
            st.last_success_at = st.last_attempt_at = now
            st.last_error = None
            st.consecutive_failures = 0
            self._write_state(st)
            return diff_snapshots(current, current)
        if current is not None and not force and current.entries:
            shrink = 1 - len(snapshot.entries) / len(current.entries)
            if shrink > max_shrink_ratio:
                self.record_failure(
                    snapshot.list_id,
                    f"rejected: snapshot shrank by {shrink:.0%} "
                    f"({len(current.entries)} -> {len(snapshot.entries)}); approval required",
                    now,
                )
                raise ListShrinkError(
                    f"{snapshot.list_id}: new snapshot is {shrink:.0%} smaller than current; "
                    "re-run with force after review"
                )
        diff = diff_snapshots(current, snapshot)
        path = self.root / snapshot.list_id / "snapshots" / f"{_safe(snapshot.version)}.json.gz"
        if not path.exists():
            _atomic_write(path, gzip.compress(snapshot.model_dump_json().encode()))
        st = self.state(snapshot.list_id)
        st.current_version = snapshot.version
        st.last_success_at = now
        st.last_attempt_at = now
        st.last_error = None
        st.consecutive_failures = 0
        self._write_state(st)
        self._cache[(snapshot.list_id, snapshot.version)] = snapshot
        return diff

    def load(self, list_id: str, version: str) -> ListSnapshot:
        key = (list_id, version)
        if key not in self._cache:
            path = self.root / list_id / "snapshots" / f"{_safe(version)}.json.gz"
            self._cache[key] = ListSnapshot.model_validate_json(gzip.decompress(path.read_bytes()))
        return self._cache[key]

    def current(self, list_id: str) -> ListSnapshot | None:
        st = self.state(list_id)
        if st.current_version is None:
            return None
        return self.load(list_id, st.current_version)

    def versions(self, list_id: str) -> list[str]:
        d = self.root / list_id / "snapshots"
        if not d.is_dir():
            return []
        return sorted(p.name.removesuffix(".json.gz") for p in d.glob("*.json.gz"))

    def list_ids(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if (p / "state.json").is_file())

    def status(self, list_id: str, sla_hours: float, now: datetime | None = None) -> ListStatus:
        now = now or datetime.now(UTC)
        st = self.state(list_id)
        snap = self.current(list_id)
        age = None
        if st.last_success_at is not None:
            age = max(0.0, (now - st.last_success_at).total_seconds() / 3600)
        return ListStatus(
            list_id=list_id,
            list_type=snap.list_type if snap else None,
            version=st.current_version,
            published_at=snap.published_at if snap else None,
            last_success_at=st.last_success_at,
            age_hours=round(age, 2) if age is not None else None,
            sla_hours=sla_hours,
            stale=age is None or age > sla_hours,
            entries=len(snap.entries) if snap else 0,
            last_error=st.last_error,
        )
