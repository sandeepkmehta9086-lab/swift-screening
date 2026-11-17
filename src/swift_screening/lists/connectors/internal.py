"""Generic JSON / CSV connector for internal (tenant-private) lists and test fixtures.

JSON: {"list_id": "...", "list_type": "internal", "published_at": "ISO-8601",
       "entries": [WatchlistEntry-like objects without list_id]}
CSV columns: entry_id,name,entry_type,aliases(;-separated),countries(;),dates_of_birth(;),
             programs(;),bic(;),lei(;),imo(;),passport(;)
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import ClassVar

from pydantic import ValidationError

from swift_screening.lists.connectors.base import ListConnector, ListFormatError
from swift_screening.lists.model import EntryIdentifier, ListType, WatchlistEntry


class JsonListConnector(ListConnector):
    def __init__(self, list_id: str, list_type: ListType = ListType.INTERNAL) -> None:
        self.list_id = list_id
        self.list_type = list_type

    def parse(self, data: bytes) -> tuple[list[WatchlistEntry], datetime | None]:
        try:
            doc = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ListFormatError(f"{self.list_id}: invalid JSON: {exc}") from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
            raise ListFormatError(f"{self.list_id}: JSON must be an object with 'entries'")
        if doc.get("list_type"):
            self.list_type = ListType(doc["list_type"])
        published = datetime.fromisoformat(doc["published_at"]) if doc.get("published_at") else None
        entries = []
        for i, raw in enumerate(doc["entries"]):
            try:
                entries.append(
                    WatchlistEntry.model_validate(
                        {"list_type": self.list_type, **raw, "list_id": self.list_id}
                    )
                )
            except ValidationError as exc:
                raise ListFormatError(f"{self.list_id}: entry #{i} invalid: {exc}") from exc
        return entries, published


class CsvListConnector(ListConnector):
    REQUIRED: ClassVar[set[str]] = {"entry_id", "name"}

    def __init__(self, list_id: str, list_type: ListType = ListType.INTERNAL) -> None:
        self.list_id = list_id
        self.list_type = list_type

    def parse(self, data: bytes) -> tuple[list[WatchlistEntry], datetime | None]:
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
        if not reader.fieldnames or not set(reader.fieldnames) >= self.REQUIRED:
            raise ListFormatError(f"{self.list_id}: CSV needs columns {sorted(self.REQUIRED)}")

        def multi(row: dict[str, str], col: str) -> list[str]:
            return [v.strip() for v in (row.get(col) or "").split(";") if v.strip()]

        entries = []
        for n, row in enumerate(reader, start=2):
            if not (row.get("entry_id") or "").strip() or not (row.get("name") or "").strip():
                raise ListFormatError(f"{self.list_id}: line {n} missing entry_id or name")
            idents = [
                EntryIdentifier(type=t.upper(), value=v)
                for t in ("bic", "lei", "imo", "passport")
                for v in multi(row, t)
            ]
            entries.append(
                WatchlistEntry(
                    list_id=self.list_id,
                    list_type=self.list_type,
                    entry_id=row["entry_id"].strip(),
                    name=row["name"].strip(),
                    entry_type=(row.get("entry_type") or "unknown").strip() or "unknown",
                    aliases=multi(row, "aliases"),
                    countries=multi(row, "countries"),
                    dates_of_birth=multi(row, "dates_of_birth"),
                    programs=multi(row, "programs"),
                    identifiers=idents,
                )
            )
        return entries, None
