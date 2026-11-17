"""Contract gates: checked-in OpenAPI spec is current; every Phase 1 scenario has a test."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import export_openapi  # noqa: E402
import scenario_coverage  # noqa: E402


def test_openapi_spec_is_current() -> None:
    current = export_openapi.TARGET.read_text(encoding="utf-8")
    assert current == export_openapi.render(), "run: uv run python tools/export_openapi.py"


def test_openapi_has_contract_fields() -> None:
    spec = export_openapi.build_spec()
    assert spec["openapi"].startswith("3.1")
    result = spec["components"]["schemas"]["ScreeningResult"]["properties"]
    for field in (
        "screening_id",
        "decision",
        "risk_score",
        "list_versions",
        "hits",
        "model_version",
        "latency_ms",
        "reason_codes",
        "config_version",
    ):
        assert field in result, field
    hit = spec["components"]["schemas"]["Hit"]["properties"]
    for field in ("field", "input_value", "matched_entry", "match_type", "score", "explanation"):
        assert field in hit, field


def test_every_phase1_scenario_has_a_test() -> None:
    found, _ = scenario_coverage.collect()
    missing = [s for s in scenario_coverage.PHASE1 if s not in found]
    assert not missing, f"Phase 1 scenarios without tests: {missing}"
