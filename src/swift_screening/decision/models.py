"""Screening result contract (mirrors the roadmap's response contract, plus audit fields)."""

from __future__ import annotations

import builtins
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Decision(StrEnum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"

    @property
    def severity(self) -> int:
        return {"PASS": 0, "REVIEW": 1, "BLOCK": 2}[self.value]

    @staticmethod
    def worst(*decisions: Decision) -> Decision:
        return max(decisions, key=lambda d: d.severity, default=Decision.PASS)


class MatchedEntry(BaseModel):
    list: str
    id: str
    name: str
    matched_name: str
    matched_name_kind: str
    list_type: str
    entry_type: str
    programs: builtins.list[str] = Field(default_factory=builtins.list)  # "list" is a field


class Hit(BaseModel):
    hit_id: str
    field: str
    role: str | None = None
    input_value: str | None  # None in stateless mode
    matched_entry: MatchedEntry
    match_type: list[str]
    candidate_sources: list[str]
    score: float | None  # None when bucketed (public mode)
    recall_floor_protected: bool = True
    action: str
    explanation: str = ""


class GeoFinding(BaseModel):
    rule_id: str
    kind: str
    field: str
    input_value: str | None
    matched: str
    method: str
    action: str
    explanation: str = ""


class ParseIssue(BaseModel):
    code: str
    message: str
    path: str | None = None
    severity: str


class ScreeningResult(BaseModel):
    screening_id: str
    tenant_id: str
    decision: Decision
    reason_codes: list[str]
    risk_score: float | None
    message_type: str | None
    message_id: str | None = None
    uetr: str | None = None
    input_hash: str
    hits: list[Hit] = Field(default_factory=list)
    geo_findings: list[GeoFinding] = Field(default_factory=list)
    parse_issues: list[ParseIssue] = Field(default_factory=list)
    list_versions: dict[str, str]
    stale_lists: list[str] = Field(default_factory=list)
    model_version: str = "deterministic-v1"
    rule_set_version: str
    config_version: str
    cpm_version: str | None = None
    engine_version: str
    stateless: bool
    summary: str = ""
    created_at: datetime
    latency_ms: float | None = None
    idempotent_replay: bool = False
