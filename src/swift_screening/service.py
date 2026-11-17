"""Screening orchestration: parse -> extract -> match -> geo -> policy -> explain -> persist.

Every path out of ``screen_*`` yields a ScreeningResult. Errors never yield PASS: they produce a
fail-closed REVIEW with a reason code (PARSE_FAILED, INTERNAL_ERROR, TIMEOUT, LIST_UNAVAILABLE).
The only exception is storage being unavailable, which raises ``StorageUnavailable`` so the API
can answer 503 instead of returning an un-audited decision.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import structlog

from swift_screening import __version__
from swift_screening.adapters import PassThroughScorer, apply_scorer
from swift_screening.config.models import AppConfig, TenantConfig
from swift_screening.cpm.models import (
    CPM_VERSION,
    CanonicalPayment,
    Identifier,
    IdentifierType,
    Party,
    PartyRole,
    Provenance,
)
from swift_screening.decision.explain import explain_geo, explain_hit
from swift_screening.decision.models import (
    Decision,
    GeoFinding,
    Hit,
    MatchedEntry,
    ParseIssue,
    ScreeningResult,
)
from swift_screening.decision.policy import RULE_SET_VERSION, PolicyContext, evaluate
from swift_screening.decision.reason_codes import ReasonCode
from swift_screening.extraction.subjects import Subject, extract_subjects
from swift_screening.lists.model import ListSnapshot
from swift_screening.lists.store import ListStore
from swift_screening.matching.engine import Matcher, RawHit
from swift_screening.matching.geo import GeoHit, GeoRules
from swift_screening.matching.index import WatchlistIndex
from swift_screening.observability import metrics
from swift_screening.parsing.errors import ParseError
from swift_screening.parsing.mt.parser import parse_mt
from swift_screening.parsing.mx.parser import parse_mx
from swift_screening.storage.db import Database
from swift_screening.storage.redact import redact_result

if TYPE_CHECKING:
    from swift_screening.interfaces import Scorer

log = structlog.get_logger("swift_screening")
ENGINE_VERSION = f"swift-screening-{__version__}"
Format = Literal["MT", "MX", "ENTITY"]
STATE_TTL_SECONDS = 5.0


@dataclass(slots=True)
class EntityRequest:
    name: str
    country: str | None = None
    date_of_birth: str | None = None
    entity_type: str = "unknown"
    identifiers: list[dict[str, str]] | None = None


class ScreeningService:
    def __init__(self, store: ListStore, config: AppConfig, db: Database | None = None) -> None:
        self.store = store
        self.config = config
        self.db = db
        self.scorer: Scorer = PassThroughScorer()
        self._index_cache: dict[tuple[tuple[str, str], ...], WatchlistIndex] = {}
        self._versions_cache: dict[str, tuple[float, str | None]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lists / index
    def _current_version(self, list_id: str) -> str | None:
        now = time.monotonic()
        cached = self._versions_cache.get(list_id)
        if cached and now - cached[0] < STATE_TTL_SECONDS:
            return cached[1]
        version = self.store.state(list_id).current_version
        self._versions_cache[list_id] = (now, version)
        return version

    def invalidate(self) -> None:
        self._versions_cache.clear()

    def resolve_versions(
        self, tenant: TenantConfig, pinned: dict[str, str] | None = None
    ) -> tuple[dict[str, str], list[str]]:
        versions: dict[str, str] = {}
        missing: list[str] = []
        for list_id in tenant.lists.enabled:
            v = pinned.get(list_id) if pinned is not None else self._current_version(list_id)
            if v is None:
                missing.append(list_id)
            else:
                versions[list_id] = v
        return versions, missing

    def index_for(self, versions: dict[str, str]) -> WatchlistIndex:
        key = tuple(sorted(versions.items()))
        with self._lock:
            idx = self._index_cache.get(key)
            if idx is None:
                snaps: list[ListSnapshot] = [self.store.load(k, v) for k, v in key]
                idx = WatchlistIndex(snaps)
                if len(self._index_cache) >= 4:  # keep a few versions for replays
                    self._index_cache.pop(next(iter(self._index_cache)))
                self._index_cache[key] = idx
            return idx

    def stale_lists(self, tenant: TenantConfig, versions: dict[str, str]) -> list[str]:
        stale = []
        for list_id in versions:
            st = self.store.status(list_id, tenant.lists.sla_for(list_id))
            metrics.LIST_AGE.labels(list_id).set(st.age_hours or -1)
            if st.stale:
                stale.append(list_id)
        return stale

    # ------------------------------------------------------------------ public API
    def screen_mt(self, raw: str, tenant: TenantConfig, **kw: Any) -> ScreeningResult:
        return self._screen("MT", raw.encode("utf-8", "surrogatepass"), tenant, **kw)

    def screen_mx(self, raw: bytes | str, tenant: TenantConfig, **kw: Any) -> ScreeningResult:
        data = raw.encode("utf-8") if isinstance(raw, str) else raw
        return self._screen("MX", data, tenant, **kw)

    def screen_entity(self, req: EntityRequest, tenant: TenantConfig, **kw: Any) -> ScreeningResult:
        body = json.dumps(dataclasses.asdict(req), sort_keys=True, default=str).encode()
        return self._screen("ENTITY", body, tenant, entity=req, **kw)

    def fail_closed(
        self, fmt: Format, raw: bytes, tenant: TenantConfig, reason: ReasonCode
    ) -> ScreeningResult:
        """Build + persist a REVIEW result without screening (timeouts, internal errors)."""
        versions, _ = self.resolve_versions(tenant)
        input_hash = hashlib.sha256(raw).hexdigest()
        result = self._result(
            tenant,
            fmt,
            input_hash,
            None,
            [],
            [],
            [],
            versions,
            [],
            PolicyContext(forced_reasons=[reason]),
            None,
            t0=time.perf_counter(),
        )
        self._persist(result, tenant, f"failclosed-{uuid.uuid4().hex}")
        return result

    # ------------------------------------------------------------------ core
    def _screen(
        self,
        fmt: Format,
        raw: bytes,
        tenant: TenantConfig,
        *,
        entity: EntityRequest | None = None,
        list_versions: dict[str, str] | None = None,
    ) -> ScreeningResult:
        t0 = time.perf_counter()
        input_hash = hashlib.sha256(raw).hexdigest()
        versions, missing = self.resolve_versions(tenant, list_versions)
        idem_key = hashlib.sha256(
            json.dumps(
                [
                    tenant.tenant_id,
                    fmt,
                    input_hash,
                    tenant.config_version,
                    sorted(versions.items()),
                    ENGINE_VERSION,
                ]
            ).encode()
        ).hexdigest()
        if self.db is not None and list_versions is None:
            existing = self.db.find_by_idempotency(tenant.tenant_id, idem_key)
            if existing is not None:
                prior = ScreeningResult.model_validate_json(existing)
                prior.idempotent_replay = True
                return prior

        try:
            return self._screen_inner(
                fmt,
                raw,
                tenant,
                entity,
                versions,
                missing,
                input_hash,
                idem_key,
                t0,
                pinned=list_versions is not None,
            )
        except Exception as exc:
            from swift_screening.storage.db import StorageUnavailable

            if isinstance(exc, StorageUnavailable):
                raise
            log.error(
                "screening_internal_error", error_type=type(exc).__name__, tenant=tenant.tenant_id
            )
            result = self._result(
                tenant,
                fmt,
                input_hash,
                None,
                [],
                [],
                [],
                versions,
                [],
                PolicyContext(forced_reasons=[ReasonCode.INTERNAL_ERROR]),
                None,
                t0=t0,
            )
            self._persist(result, tenant, idem_key + "-err-" + uuid.uuid4().hex[:8])
            return result

    def _screen_inner(
        self,
        fmt: Format,
        raw: bytes,
        tenant: TenantConfig,
        entity: EntityRequest | None,
        versions: dict[str, str],
        missing: list[str],
        input_hash: str,
        idem_key: str,
        t0: float,
        *,
        pinned: bool,
    ) -> ScreeningResult:
        cpm: CanonicalPayment | None = None
        issues: list[ParseIssue] = []
        if fmt == "ENTITY":
            assert entity is not None
            subjects = _entity_subjects(entity)
        else:
            try:
                cpm = parse_mt(raw.decode("utf-8", "replace")) if fmt == "MT" else parse_mx(raw)
                subjects = extract_subjects(cpm)
                issues = [
                    ParseIssue(code=w.code, message=w.message, path=w.path, severity=w.severity)
                    for w in cpm.parse_warnings
                ]
            except ParseError as exc:
                issues = [
                    ParseIssue(
                        code="PARSE_FAILED", message=str(exc), path=exc.path, severity="error"
                    )
                ]
                subjects = _degraded_subjects(raw)

        index = self.index_for(versions)
        matcher = Matcher(index, tenant.thresholds.review)
        hits, _stats = matcher.screen(subjects)
        # Precision layer slot (Phase 2): always runs under the recall-floor guard.
        hits = apply_scorer(hits, index.entries, self.scorer, tenant.thresholds.review)

        geo = GeoRules(tenant.embargo_countries, tenant.embargo_regions)
        geo_hits = _geo(geo, subjects)

        if versions:
            missing_lists = [m for m in missing if m in tenant.lists.required]
        else:  # nothing loaded at all: never screen "clean" against an empty index
            missing_lists = missing or ["<no lists enabled>"]
        ctx = PolicyContext(
            parse_errors=any(i.severity == "error" for i in issues),
            stale_lists=[] if pinned else self.stale_lists(tenant, versions),
            missing_lists=missing_lists,
            possible_duplicate=bool(cpm and cpm.possible_duplicate),
        )
        result = self._result(
            tenant,
            fmt,
            input_hash,
            cpm,
            issues,
            hits,
            geo_hits,
            versions,
            index.entries,
            ctx,
            index,
            t0=t0,
        )
        if not pinned:
            self._persist(result, tenant, idem_key)
        return result

    def _result(
        self,
        tenant: TenantConfig,
        fmt: Format,
        input_hash: str,
        cpm: CanonicalPayment | None,
        issues: list[ParseIssue],
        hits: list[RawHit],
        geo_hits: list[GeoHit],
        versions: dict[str, str],
        entries: list[Any],
        ctx: PolicyContext,
        index: WatchlistIndex | None,
        *,
        t0: float,
    ) -> ScreeningResult:
        outcome = evaluate(hits, entries, geo_hits, ctx, tenant)
        stateless = tenant.stateless or tenant.public_mode
        out_hits: list[Hit] = []
        for h, action in zip(hits, outcome.hit_actions, strict=True):
            if action == Decision.PASS:
                continue
            e = entries[h.entry_idx]
            out_hits.append(
                Hit(
                    hit_id=f"hit_{len(out_hits) + 1}",
                    field=h.subject.path,
                    role=h.subject.role,
                    input_value=None if stateless else h.matched_input,
                    matched_entry=MatchedEntry(
                        list=e.list_id,
                        id=e.entry_id,
                        name=e.name,
                        matched_name=h.name.raw if h.name else (h.identifier or e.name),
                        matched_name_kind=h.name.kind if h.name else "identifier",
                        list_type=e.list_type.value,
                        entry_type=e.entry_type,
                        programs=e.programs,
                    ),
                    match_type=sorted(h.methods),
                    candidate_sources=sorted(h.sources),
                    score=None if tenant.public_mode else round(h.score, 4),
                    action=action.value,
                    explanation=explain_hit(h, e, action, stateless=stateless),
                )
            )
        findings = [
            GeoFinding(
                rule_id=g.rule_id,
                kind=g.kind,
                field=g.path,
                input_value=None if stateless else g.input_value,
                matched=g.matched,
                method=g.method,
                action=a.value,
                explanation=explain_geo(g, stateless=stateless),
            )
            for g, a in zip(geo_hits, outcome.geo_actions, strict=True)
        ]
        scores = [
            h.score for h, a in zip(hits, outcome.hit_actions, strict=True) if a != Decision.PASS
        ]
        risk = max([*scores, *([1.0] if findings else [])], default=0.0)
        result = ScreeningResult(
            screening_id=f"scr_{uuid.uuid4().hex[:24]}",
            tenant_id=tenant.tenant_id,
            decision=outcome.decision,
            reason_codes=[r.value for r in outcome.reasons],
            risk_score=None if tenant.public_mode else round(risk, 4),
            message_type=cpm.message_type if cpm else (None if fmt != "ENTITY" else "ENTITY"),
            message_id=None if stateless or cpm is None else cpm.message_id,
            uetr=cpm.uetr if cpm else None,
            input_hash=input_hash,
            hits=out_hits,
            geo_findings=findings,
            parse_issues=issues,
            list_versions=versions,
            stale_lists=ctx.stale_lists,
            rule_set_version=RULE_SET_VERSION,
            config_version=tenant.config_version,
            cpm_version=CPM_VERSION if cpm else None,
            engine_version=ENGINE_VERSION,
            stateless=stateless,
            summary=_summary(outcome.decision, out_hits, findings),
            created_at=datetime.now(UTC),
        )
        result.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        metrics.SCREENINGS.labels(tenant.tenant_id, result.decision.value, fmt).inc()
        metrics.LATENCY.labels(fmt).observe(result.latency_ms / 1000)
        for r in result.reason_codes:
            metrics.REASONS.labels(r).inc()
        log.info(
            "screened",
            screening_id=result.screening_id,
            tenant=tenant.tenant_id,
            decision=result.decision.value,
            reasons=result.reason_codes,
            hits=len(out_hits),
            latency_ms=result.latency_ms,
        )
        return result

    def _persist(self, result: ScreeningResult, tenant: TenantConfig, idem_key: str) -> None:
        if self.db is None:
            return
        data = result.model_dump(mode="json")
        stateless = tenant.stateless or tenant.public_mode
        if stateless:
            data = redact_result(data)
        audit = {
            "screening_id": result.screening_id,
            "input_hash": result.input_hash,
            "decision": result.decision.value,
            "reason_codes": result.reason_codes,
            "list_versions": result.list_versions,
            "config_version": result.config_version,
            "rule_set_version": result.rule_set_version,
            "model_version": result.model_version,
            "engine_version": result.engine_version,
            "hits": [
                f"{h.matched_entry.list}:{h.matched_entry.id}:{h.action}" for h in result.hits
            ],
            "geo": [f"{g.rule_id}:{g.action}" for g in result.geo_findings],
        }
        saved = self.db.save_result_with_audit(
            screening_id=result.screening_id,
            tenant_id=tenant.tenant_id,
            idempotency_key=idem_key,
            input_hash=result.input_hash,
            decision=result.decision.value,
            result_json=json.dumps(data),
            redacted=stateless,
            audit_payload=audit,
            created_at=result.created_at,
        )
        if not saved:  # concurrent duplicate submission: return the stored one
            existing = self.db.find_by_idempotency(tenant.tenant_id, idem_key)
            if existing:
                stored = ScreeningResult.model_validate_json(existing)
                result.screening_id = stored.screening_id
                result.idempotent_replay = True

    def get_result(self, tenant: TenantConfig, screening_id: str) -> ScreeningResult | None:
        if self.db is None:
            return None
        raw = self.db.get_result(tenant.tenant_id, screening_id)
        return ScreeningResult.model_validate_json(raw) if raw else None


# ---------------------------------------------------------------------- helpers
def _entity_subjects(req: EntityRequest) -> list[Subject]:
    party = Party(
        role=PartyRole.MENTIONED,
        name=req.name,
        name_lines=[req.name],
        country=req.country,
        party_type=req.entity_type
        if req.entity_type in {"individual", "organisation"}
        else "unknown",
        provenance=[Provenance(source_format="MX", message_type="entity", path="ENTITY:name")],
    )
    for ident in req.identifiers or []:
        try:
            id_type = IdentifierType(ident.get("type", "OTHER").upper())
        except ValueError:
            id_type = IdentifierType.OTHER
        party.identifiers.append(Identifier(type=id_type, value=ident.get("value", "")))
    cpm = CanonicalPayment(source_format="MX", message_type="entity", raw_hash="", parties=[party])
    return extract_subjects(cpm)


def _degraded_subjects(raw: bytes) -> list[Subject]:
    """Parse failed: still screen every line of the raw input (fail closed, but informed)."""
    text = raw.decode("utf-8", "replace")
    out: list[Subject] = []
    for i, line in enumerate(text.splitlines()[:2000]):
        line = line.strip()
        if not line:
            continue
        if line.startswith(":") and line.count(":") >= 2:
            line = line.split(":", 2)[2]
        line = line.replace("<", " <").strip()
        out.append(Subject("text", line, f"RAW:line{i}", origin="free_text"))
        if len(line) <= 140:
            out.append(Subject("name", line, f"RAW:line{i}", origin="free_text_rule"))
    return out


def _geo(geo: GeoRules, subjects: list[Subject]) -> list[GeoHit]:
    found: dict[tuple[str, str], GeoHit] = {}
    for s in subjects:
        hits: list[GeoHit] = []
        if s.kind == "country":
            if (h := geo.check_code(s.value, s.path)) is not None:
                hits.append(h)
        elif s.kind == "text":
            hits.extend(geo.check_text(s.value, s.path))
        for h in hits:
            found.setdefault((h.rule_id, h.path.split("#")[0].split("/line")[0]), h)
    return list(found.values())


def _summary(decision: Decision, hits: list[Hit], geo: list[GeoFinding]) -> str:
    if decision == Decision.PASS:
        return "No match at or above the review threshold."
    parts = []
    if hits:
        lists = sorted({h.matched_entry.list for h in hits})
        parts.append(f"{len(hits)} watchlist hit(s) on {', '.join(lists)}")
    if geo:
        parts.append(f"{len(geo)} embargo/region finding(s)")
    return "; ".join(parts) or "Held for review (fail closed)."
