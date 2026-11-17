from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

SCREENINGS = Counter(
    "swift_screening_decisions_total",
    "Screening decisions",
    ["tenant", "decision", "format"],
    registry=REGISTRY,
)
REASONS = Counter(
    "swift_screening_reasons_total", "Reason codes emitted", ["reason"], registry=REGISTRY
)
LATENCY = Histogram(
    "swift_screening_latency_seconds",
    "End-to-end screening latency",
    ["format"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.25, 0.5, 1, 2, 5),
    registry=REGISTRY,
)
LIST_AGE = Gauge(
    "swift_screening_list_age_hours",
    "Hours since last successful list refresh",
    ["list_id"],
    registry=REGISTRY,
)
LIST_ENTRIES = Gauge(
    "swift_screening_list_entries",
    "Entries in the current list snapshot",
    ["list_id"],
    registry=REGISTRY,
)
