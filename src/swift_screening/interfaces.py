"""Component interfaces (Agent.md Phase 0, task 5).

Every pluggable part of the pipeline is defined here as a runtime-checkable Protocol, so later
phases can swap implementations (OPA policy, ML scorer, LLM extractor, Kafka queue, WORM audit
store) without changing callers. ``tests/unit/test_interfaces.py`` asserts that the shipped
implementations conform.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from swift_screening.config.models import TenantConfig
from swift_screening.cpm.models import CanonicalPayment
from swift_screening.extraction.subjects import Subject
from swift_screening.lists.model import ListSnapshot, WatchlistEntry
from swift_screening.matching.engine import MatchStats, RawHit
from swift_screening.matching.geo import GeoHit
from swift_screening.matching.normalize import NormalizedName


@runtime_checkable
class MessageParser(Protocol):
    """Raw message -> Canonical Payment Model. Raises ParseError only if nothing is usable."""

    source_format: str

    def parse(self, raw: bytes | str) -> CanonicalPayment: ...


@runtime_checkable
class EntityExtractor(Protocol):
    """CPM -> screening subjects (names, free text, identifiers, countries) with provenance."""

    def extract(self, cpm: CanonicalPayment) -> list[Subject]: ...


@runtime_checkable
class ListSource(Protocol):
    """Watchlist connector: raw source bytes -> versioned snapshot."""

    list_id: str
    default_url: str | None

    def snapshot(self, data: bytes, source: str) -> ListSnapshot: ...


@runtime_checkable
class CandidateIndex(Protocol):
    """Recall-safe candidate retrieval over list names and identifiers."""

    entries: list[WatchlistEntry]

    def name_candidates(self, query: NormalizedName) -> list[int]: ...

    def lookup_identifier(self, id_type: str, value: str) -> list[int]: ...


@runtime_checkable
class Matcher(Protocol):
    """Subjects -> raw hits at or above the recall floor."""

    def screen(self, subjects: list[Subject]) -> tuple[list[RawHit], MatchStats]: ...


@runtime_checkable
class Scorer(Protocol):
    """Precision layer (Phase 2): re-scores candidates using context. It may rank and add
    evidence but must never push a hit below the recall floor (see ``apply_scorer``)."""

    version: str

    def score(self, hits: Sequence[RawHit], entries: Sequence[WatchlistEntry]) -> list[float]: ...


@runtime_checkable
class PolicyEngine(Protocol):
    """Hits + context -> decision and reason codes (Python now, OPA/Rego in Phase 3)."""

    version: str

    def evaluate(
        self,
        hits: list[RawHit],
        entries: list[WatchlistEntry],
        geo_hits: list[GeoHit],
        ctx: Any,
        tenant: TenantConfig,
    ) -> Any: ...


@runtime_checkable
class Explainer(Protocol):
    """Human-readable rationale built only from structured evidence."""

    def explain_hit(
        self, hit: RawHit, entry: WatchlistEntry, action: Any, *, stateless: bool
    ) -> str: ...

    def explain_geo(self, geo: GeoHit, *, stateless: bool) -> str: ...


@runtime_checkable
class LLMProvider(Protocol):
    """Pluggable LLM (Phase 2). Nothing in Phase 1 requires one; the default is NullLLMProvider.
    ``external`` providers may only receive payment data if the tenant allows it."""

    name: str
    external: bool

    def complete(
        self, prompt: str, *, system: str = "", max_tokens: int = 512, timeout_s: float = 10.0
    ) -> str: ...


@runtime_checkable
class AuditSink(Protocol):
    """Append-only, tamper-evident audit trail."""

    def append_audit(
        self, tenant_id: str, event_type: str, ref_id: str | None, payload: dict[str, Any]
    ) -> None: ...

    def verify_chain(self) -> tuple[bool, int | None, int]: ...


@runtime_checkable
class JobQueue(Protocol):
    """Async work queue (batch screening, Phase 2; Redis/Kafka later)."""

    def enqueue(self, job_type: str, payload: dict[str, Any]) -> str: ...

    def dequeue(self) -> tuple[str, str, dict[str, Any]] | None: ...

    def complete(self, job_id: str, result: dict[str, Any]) -> None: ...

    def status(self, job_id: str) -> dict[str, Any] | None: ...
