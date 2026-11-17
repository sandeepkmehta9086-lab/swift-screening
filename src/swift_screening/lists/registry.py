"""Connector registry and the refresh routine (fetch -> parse -> validate -> diff -> store)."""

from __future__ import annotations

from swift_screening.lists.connectors.base import (
    ListConnector,
    ListFetchError,
    ListFormatError,
    fetch_source,
)
from swift_screening.lists.connectors.internal import CsvListConnector, JsonListConnector
from swift_screening.lists.connectors.official import (
    EuFsfConnector,
    OfacSdnConnector,
    UnConsolidatedConnector,
)
from swift_screening.lists.model import ListType, SnapshotDiff
from swift_screening.lists.store import ListShrinkError, ListStore

FORMATS = {"ofac", "un", "eu", "json", "csv"}


def connector_for(
    fmt: str, list_id: str | None = None, list_type: ListType = ListType.INTERNAL
) -> ListConnector:
    if fmt == "ofac":
        c: ListConnector = OfacSdnConnector()
    elif fmt == "un":
        c = UnConsolidatedConnector()
    elif fmt == "eu":
        c = EuFsfConnector()
    elif fmt == "json":
        c = JsonListConnector(list_id or "INTERNAL", list_type)
    elif fmt == "csv":
        c = CsvListConnector(list_id or "INTERNAL", list_type)
    else:
        raise ValueError(f"unknown list format {fmt!r}; expected one of {sorted(FORMATS)}")
    if list_id and fmt in {"ofac", "un", "eu"}:
        c.list_id = list_id
    return c


def refresh_list(
    store: ListStore,
    connector: ListConnector,
    source: str | None = None,
    *,
    force: bool = False,
    max_shrink_ratio: float = 0.3,
    retries: int = 3,
    backoff_seconds: float = 2.0,
) -> SnapshotDiff:
    """Fetch and store a new snapshot. On any failure the last good snapshot stays current."""
    location = source or connector.default_url
    if not location:
        raise ListFetchError(f"{connector.list_id}: no source given")
    try:
        data = fetch_source(location, retries=retries, backoff_seconds=backoff_seconds)
        snapshot = connector.snapshot(data, location)
        return store.save(snapshot, force=force, max_shrink_ratio=max_shrink_ratio)
    except ListShrinkError:
        raise  # failure already recorded by the store
    except (ListFetchError, ListFormatError) as exc:
        store.record_failure(connector.list_id, f"{type(exc).__name__}: {exc}")
        raise
