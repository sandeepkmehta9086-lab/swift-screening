"""Tenant configuration. Every result records the ``config_version`` (hash of the tenant config)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

Action = Literal["REVIEW", "BLOCK"]


class Thresholds(BaseModel):
    review: float = Field(0.82, ge=0.5, le=1.0)  # also the recall floor in Phase 1
    block: float = Field(0.98, ge=0.5, le=1.01)  # >1.0 disables name-based auto-block
    # Name-only auto-BLOCK also needs this many distinctive words in the matched list name.
    # Shorter names ("Mohammed Ahmed", "Global Trading") are common and go to REVIEW.
    block_min_name_tokens: int = Field(3, ge=1, le=10)

    @model_validator(mode="after")
    def _order(self) -> Thresholds:
        if self.block < self.review:
            raise ValueError("block threshold must be >= review threshold")
        return self


class EmbargoCountry(BaseModel):
    code: str
    names: list[str] = Field(default_factory=list)
    action: Action = "BLOCK"

    @field_validator("code")
    @classmethod
    def _iso2(cls, v: str) -> str:
        if len(v) != 2 or not v.isalpha():
            raise ValueError("country code must be ISO 3166-1 alpha-2")
        return v.upper()


class EmbargoRegion(BaseModel):
    id: str
    country: str | None = None
    names: list[str]
    action: Action = "REVIEW"


class ListSettings(BaseModel):
    enabled: list[str] = Field(default_factory=lambda: ["OFAC_SDN", "UN_CONSOLIDATED", "EU_FSF"])
    required: list[str] = Field(default_factory=list)  # missing => LIST_UNAVAILABLE (REVIEW)
    sla_hours: dict[str, float] = Field(default_factory=dict)
    default_sla_hours: float = 36.0
    stale_action: Literal["hold", "flag"] = "hold"

    def sla_for(self, list_id: str) -> float:
        return self.sla_hours.get(list_id, self.default_sla_hours)


class TenantConfig(BaseModel):
    tenant_id: str
    name: str = ""
    api_key_sha256: list[str] = Field(default_factory=list)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    lists: ListSettings = Field(default_factory=ListSettings)
    embargo_countries: list[EmbargoCountry] = Field(default_factory=list)
    embargo_regions: list[EmbargoRegion] = Field(default_factory=list)
    identifier_hit_action: Action = "BLOCK"
    pep_action: Action = "REVIEW"
    internal_list_action: Action = "REVIEW"
    stateless: bool = False
    public_mode: bool = False  # score bucketing: decision only, no exact scores
    rate_limit_per_minute: int = 600
    max_body_bytes: int = 1_000_000
    screening_timeout_ms: int = 3000

    @property
    def config_version(self) -> str:
        data = self.model_dump(mode="json", exclude={"api_key_sha256", "name"})
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        return f"cfg-{digest[:12]}"


class AppConfig(BaseModel):
    tenants: list[TenantConfig]

    @model_validator(mode="after")
    def _unique(self) -> AppConfig:
        ids = [t.tenant_id for t in self.tenants]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate tenant_id")
        keys = [k for t in self.tenants for k in t.api_key_sha256]
        if len(keys) != len(set(keys)):
            raise ValueError("an API key hash is assigned to more than one tenant")
        return self

    def tenant(self, tenant_id: str) -> TenantConfig | None:
        return next((t for t in self.tenants if t.tenant_id == tenant_id), None)

    def tenant_for_key(self, api_key: str) -> TenantConfig | None:
        digest = hashlib.sha256(api_key.encode()).hexdigest()
        return next((t for t in self.tenants if digest in t.api_key_sha256), None)


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    defaults = raw.get("defaults", {}) if isinstance(raw, dict) else {}
    tenants = []
    for t in raw.get("tenants", []):
        merged = {**defaults, **t}
        tenants.append(TenantConfig.model_validate(merged))
    return AppConfig(tenants=tenants)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()
