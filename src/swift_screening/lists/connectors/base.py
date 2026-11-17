"""List connector interface, fetch with retry, and shared XML helpers."""

from __future__ import annotations

import io
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import httpx
from lxml import etree

from swift_screening.lists.model import ListSnapshot, ListType, WatchlistEntry, content_hash


class ListFormatError(Exception):
    """The source file is malformed, truncated or not the expected format."""


class ListFetchError(Exception):
    """The source could not be fetched after retries."""


class ListConnector(ABC):
    list_id: str
    list_type: ListType = ListType.SANCTIONS
    default_url: str | None = None
    min_expected_entries: int = 1

    @abstractmethod
    def parse(self, data: bytes) -> tuple[list[WatchlistEntry], datetime | None]:
        """Parse raw source bytes into entries and the publication timestamp."""

    def snapshot(self, data: bytes, source: str) -> ListSnapshot:
        try:
            entries, published = self.parse(data)
        except ListFormatError:
            raise
        except (etree.XMLSyntaxError, ValueError, KeyError, UnicodeDecodeError) as exc:
            raise ListFormatError(f"{self.list_id}: cannot parse source: {exc}") from exc
        if len(entries) < self.min_expected_entries:
            raise ListFormatError(
                f"{self.list_id}: only {len(entries)} entries parsed "
                f"(expected >= {self.min_expected_entries})"
            )
        ids = [e.entry_id for e in entries]
        if len(ids) != len(set(ids)):
            raise ListFormatError(f"{self.list_id}: duplicate entry ids in source")
        return ListSnapshot(
            list_id=self.list_id,
            list_type=self.list_type,
            source=source,
            published_at=published,
            content_hash=content_hash(entries),
            entries=entries,
        )


def fetch_source(
    location: str,
    *,
    retries: int = 3,
    backoff_seconds: float = 2.0,
    timeout: float = 60.0,
    client: httpx.Client | None = None,
) -> bytes:
    """Fetch from an http(s) URL (with retry/backoff) or read a local file path."""
    if not location.startswith(("http://", "https://")):
        path = Path(location)
        if not path.is_file():
            raise ListFetchError(f"source file not found: {location}")
        return path.read_bytes()
    owns_client = client is None
    http = client or httpx.Client(timeout=timeout, follow_redirects=True)
    last_error: Exception | None = None
    try:
        for attempt in range(retries):
            try:
                resp = http.get(location)
                resp.raise_for_status()
                return resp.content
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(backoff_seconds * (2**attempt))
    finally:
        if owns_client:
            http.close()
    raise ListFetchError(f"failed to fetch {location} after {retries} attempts: {last_error}")


# ---------------------------------------------------------------- XML helpers (namespace-agnostic)

SAFE_XML = {"resolve_entities": False, "no_network": True, "load_dtd": False, "huge_tree": True}


def local(tag: object) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def iter_records(data: bytes, record_tag: str) -> Iterator[etree._Element]:
    """Stream elements named ``record_tag`` with bounded memory."""
    if b"<!ENTITY" in data[:65536].upper():
        raise ListFormatError("entity declarations are not allowed in list files")
    ctx = etree.iterparse(io.BytesIO(data), events=("end",), **SAFE_XML)
    for _, el in ctx:
        if local(el.tag) == record_tag:
            yield el
            el.clear(keep_tail=False)


def child(el: etree._Element | None, name: str) -> etree._Element | None:
    if el is None:
        return None
    for c in el:
        if local(c.tag) == name:
            return c
    return None


def children(el: etree._Element | None, name: str) -> list[etree._Element]:
    if el is None:
        return []
    return [c for c in el if local(c.tag) == name]


def text(el: etree._Element | None, name: str | None = None) -> str | None:
    target = child(el, name) if name else el
    if target is None or target.text is None:
        return None
    return target.text.strip() or None


def descendants(el: etree._Element, name: str) -> list[etree._Element]:
    return [d for d in el.iter() if local(d.tag) == name]
