"""Phase 0: shipped implementations conform to the component interfaces; Phase 0 defaults work."""

from __future__ import annotations

import pytest

from swift_screening import interfaces as I
from swift_screening.adapters import (
    InMemoryJobQueue,
    LLMUnavailable,
    MtParser,
    MxParser,
    NullLLMProvider,
    PassThroughScorer,
    PythonPolicyEngine,
    StructuredExtractor,
    TemplateExplainer,
    apply_scorer,
)
from swift_screening.extraction.subjects import Subject
from swift_screening.lists.connectors.internal import JsonListConnector
from swift_screening.lists.connectors.official import (
    EuFsfConnector,
    OfacSdnConnector,
    UnConsolidatedConnector,
)
from swift_screening.lists.model import ListSnapshot, ListType, WatchlistEntry, content_hash
from swift_screening.matching.engine import Matcher, RawHit
from swift_screening.matching.index import WatchlistIndex
from swift_screening.storage.db import Database
from swift_screening.synth.generator import mt103, pacs008


def _index() -> WatchlistIndex:
    entries = [WatchlistEntry(list_id="T", entry_id="1", name="Kornel Vashtarin")]
    return WatchlistIndex(
        [
            ListSnapshot(
                list_id="T",
                list_type=ListType.SANCTIONS,
                source="t",
                content_hash=content_hash(entries),
                entries=entries,
            )
        ]
    )


@pytest.mark.parametrize(
    "obj,proto",
    [
        (MtParser(), I.MessageParser),
        (MxParser(), I.MessageParser),
        (StructuredExtractor(), I.EntityExtractor),
        (OfacSdnConnector(), I.ListSource),
        (UnConsolidatedConnector(), I.ListSource),
        (EuFsfConnector(), I.ListSource),
        (JsonListConnector("X"), I.ListSource),
        (_index(), I.CandidateIndex),
        (Matcher(_index(), 0.82), I.Matcher),
        (PassThroughScorer(), I.Scorer),
        (PythonPolicyEngine(), I.PolicyEngine),
        (TemplateExplainer(), I.Explainer),
        (NullLLMProvider(), I.LLMProvider),
        (Database("sqlite://"), I.AuditSink),
        (InMemoryJobQueue(), I.JobQueue),
    ],
)
def test_implementation_conforms(obj: object, proto: type) -> None:
    assert isinstance(obj, proto)


def test_parser_adapters_and_extractor() -> None:
    cpm = MtParser().parse(mt103(seed=1).encode())
    assert cpm.message_type == "MT103"
    assert MxParser().parse(pacs008()).message_type == "pacs.008.001.08"
    assert any(s.kind == "name" for s in StructuredExtractor().extract(cpm))


def test_null_llm_is_default_and_unavailable() -> None:
    llm = NullLLMProvider()
    assert llm.external is False
    with pytest.raises(LLMUnavailable):
        llm.complete("anything")


def test_job_queue_fifo_and_status() -> None:
    q = InMemoryJobQueue()
    a, b = q.enqueue("screen", {"n": 1}), q.enqueue("screen", {"n": 2})
    assert q.status(a) == {"state": "queued", "result": None}
    assert q.dequeue() == (a, "screen", {"n": 1})
    assert q.status(a)["state"] == "running"  # type: ignore[index]
    q.complete(a, {"decision": "PASS"})
    assert q.status(a) == {"state": "done", "result": {"decision": "PASS"}}
    assert q.dequeue() == (b, "screen", {"n": 2})
    assert q.dequeue() is None and q.status("nope") is None


def _hit(score: float) -> RawHit:
    return RawHit(Subject("name", "x", "MT:59"), 0, None, score, {"fuzzy"}, {"fuzzy"}, "x")


class _Suppressor:
    """A badly behaved precision model that tries to suppress every hit."""

    version = "bad-1"

    def score(self, hits, entries):  # type: ignore[no-untyped-def]
        return [0.0 for _ in hits]


@pytest.mark.scenario("G.7")
def test_scorer_can_never_push_hit_below_recall_floor() -> None:
    hits = apply_scorer([_hit(0.95), _hit(0.70)], [], _Suppressor(), floor=0.82)
    assert hits[0].score == 0.82  # protected: stays at the floor, still a REVIEW
    assert hits[1].score == 0.0  # below the floor before: the scorer may lower it


def test_scorer_failures_are_ignored() -> None:
    class Broken:
        version = "x"

        def score(self, hits, entries):  # type: ignore[no-untyped-def]
            raise RuntimeError("model file corrupt")

    class WrongLength:
        version = "y"

        def score(self, hits, entries):  # type: ignore[no-untyped-def]
            return [1.0]

    for scorer in (Broken(), WrongLength()):
        hits = apply_scorer([_hit(0.9), _hit(0.85)], [], scorer, floor=0.82)
        assert [h.score for h in hits] == [0.9, 0.85]


def test_scorer_may_raise_scores() -> None:
    class Booster:
        version = "z"

        def score(self, hits, entries):  # type: ignore[no-untyped-def]
            return [1.5 for _ in hits]

    assert [h.score for h in apply_scorer([_hit(0.9)], [], Booster(), floor=0.82)] == [1.0]
