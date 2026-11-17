"""Watchlist entry and snapshot models."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ListType(StrEnum):
    SANCTIONS = "sanctions"
    PEP = "pep"
    INTERNAL = "internal"


EntryType = Literal["individual", "organisation", "vessel", "aircraft", "unknown"]


class EntryIdentifier(BaseModel):
    type: str  # BIC, LEI, IMO, PASSPORT, NATIONAL_ID, OTHER ...
    value: str
    country: str | None = None


class WatchlistEntry(BaseModel):
    list_id: str  # e.g. "OFAC_SDN"
    entry_id: str  # source identifier (uid / reference number)
    list_type: ListType = ListType.SANCTIONS
    entry_type: EntryType = "unknown"
    name: str
    aliases: list[str] = Field(default_factory=list)
    weak_aliases: list[str] = Field(default_factory=list)  # low-quality a.k.a.; still screened
    identifiers: list[EntryIdentifier] = Field(default_factory=list)
    dates_of_birth: list[str] = Field(default_factory=list)
    nationalities: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    programs: list[str] = Field(default_factory=list)
    remarks: str | None = None
    attributes: dict[str, list[str]] = Field(default_factory=dict)  # descriptive, not matched

    @property
    def key(self) -> str:
        return f"{self.list_id}:{self.entry_id}"

    def all_names(self) -> list[tuple[str, str]]:
        """(name, kind) for primary, strong and weak aliases."""
        out = [(self.name, "primary")]
        out += [(a, "alias") for a in self.aliases if a and a != self.name]
        out += [(a, "weak_alias") for a in self.weak_aliases if a]
        return out


class ListSnapshot(BaseModel):
    list_id: str
    list_type: ListType
    source: str
    published_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    content_hash: str
    entries: list[WatchlistEntry]

    @property
    def version(self) -> str:
        stamp = (self.published_at or self.fetched_at).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{stamp}#{self.content_hash[:12]}"


def content_hash(entries: list[WatchlistEntry]) -> str:
    payload = json.dumps(
        sorted((e.model_dump(mode="json") for e in entries), key=lambda d: d["entry_id"]),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class SnapshotDiff(BaseModel):
    list_id: str
    from_version: str | None
    to_version: str
    added: list[str]
    removed: list[str]
    changed: list[str]


def diff_snapshots(old: ListSnapshot | None, new: ListSnapshot) -> SnapshotDiff:
    old_map = {e.entry_id: e for e in (old.entries if old else [])}
    new_map = {e.entry_id: e for e in new.entries}
    return SnapshotDiff(
        list_id=new.list_id,
        from_version=old.version if old else None,
        to_version=new.version,
        added=sorted(set(new_map) - set(old_map)),
        removed=sorted(set(old_map) - set(new_map)),
        changed=sorted(k for k in set(new_map) & set(old_map) if new_map[k] != old_map[k]),
    )
