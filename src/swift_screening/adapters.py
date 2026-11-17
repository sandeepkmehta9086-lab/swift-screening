"""Default (Phase 0/1) implementations of the interfaces in ``interfaces.py``.

* MtParser / MxParser, StructuredExtractor, PythonPolicyEngine, TemplateExplainer: thin
  adapters over the Phase 1 functions.
* PassThroughScorer + apply_scorer: the recall-floor guard every future scorer goes through.
* NullLLMProvider: the default; the service never needs an LLM.
* InMemoryJobQueue: for tests and single-process use.
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from collections.abc import Sequence
from typing import Any

from swift_screening.config.models import TenantConfig
from swift_screening.cpm.models import CanonicalPayment
from swift_screening.decision import explain, policy
from swift_screening.decision.models import Decision
from swift_screening.extraction.subjects import Subject, extract_subjects
from swift_screening.lists.model import WatchlistEntry
from swift_screening.matching.engine import RawHit
from swift_screening.matching.geo import GeoHit
from swift_screening.parsing.mt.parser import parse_mt
from swift_screening.parsing.mx.parser import parse_mx


class MtParser:
    source_format = "MT"

    def parse(self, raw: bytes | str) -> CanonicalPayment:
        return parse_mt(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)


class MxParser:
    source_format = "MX"

    def parse(self, raw: bytes | str) -> CanonicalPayment:
        return parse_mx(raw)


class StructuredExtractor:
    def extract(self, cpm: CanonicalPayment) -> list[Subject]:
        return extract_subjects(cpm)


class PythonPolicyEngine:
    version = policy.RULE_SET_VERSION

    def evaluate(
        self,
        hits: list[RawHit],
        entries: list[WatchlistEntry],
        geo_hits: list[GeoHit],
        ctx: Any,
        tenant: TenantConfig,
    ) -> policy.PolicyOutcome:
        return policy.evaluate(hits, entries, geo_hits, ctx, tenant)


class TemplateExplainer:
    def explain_hit(
        self, hit: RawHit, entry: WatchlistEntry, action: Decision, *, stateless: bool
    ) -> str:
        return explain.explain_hit(hit, entry, action, stateless=stateless)

    def explain_geo(self, geo: GeoHit, *, stateless: bool) -> str:
        return explain.explain_geo(geo, stateless=stateless)


class PassThroughScorer:
    """Phase 1 scorer: keeps the deterministic score."""

    version = "deterministic-v1"

    def score(self, hits: Sequence[RawHit], entries: Sequence[WatchlistEntry]) -> list[float]:
        return [h.score for h in hits]


def apply_scorer(
    hits: list[RawHit], entries: Sequence[WatchlistEntry], scorer: Any, floor: float
) -> list[RawHit]:
    """Run a scorer under the recall-floor guard (Agent.md prime directive 3).

    A hit the deterministic layer placed at/above the floor keeps a score >= floor, whatever
    the scorer says; a scorer may raise scores or re-rank, never suppress. A scorer that fails
    or returns the wrong number of scores is ignored (fail safe: deterministic scores stand)."""
    try:
        new = list(scorer.score(hits, entries))
    except Exception:
        return hits
    if len(new) != len(hits):
        return hits
    for h, s in zip(hits, new, strict=True):
        protected = h.score >= floor
        value = max(0.0, min(1.0, float(s)))
        h.score = max(value, floor) if protected else value
    return hits


class LLMUnavailable(Exception):
    """No LLM is configured or reachable. Callers fall back to rule-based behaviour."""


class NullLLMProvider:
    name = "null"
    external = False

    def complete(
        self, prompt: str, *, system: str = "", max_tokens: int = 512, timeout_s: float = 10.0
    ) -> str:
        raise LLMUnavailable("no LLM provider configured")


class InMemoryJobQueue:
    def __init__(self) -> None:
        self._pending: deque[str] = deque()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def enqueue(self, job_type: str, payload: dict[str, Any]) -> str:
        job_id = f"job_{uuid.uuid4().hex[:20]}"
        with self._lock:
            self._jobs[job_id] = {
                "type": job_type,
                "payload": payload,
                "state": "queued",
                "result": None,
            }
            self._pending.append(job_id)
        return job_id

    def dequeue(self) -> tuple[str, str, dict[str, Any]] | None:
        with self._lock:
            if not self._pending:
                return None
            job_id = self._pending.popleft()
            job = self._jobs[job_id]
            job["state"] = "running"
            return job_id, job["type"], job["payload"]

    def complete(self, job_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["state"] = "done"
            job["result"] = result

    def status(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return None if job is None else {"state": job["state"], "result": job["result"]}
