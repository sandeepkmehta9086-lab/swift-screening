"""Python policy engine: hits + context -> PASS / REVIEW / BLOCK with reason codes.

Fail-closed rules live here and are not configurable away:
  * parse errors              -> at least REVIEW (PARSE_FAILED)
  * no usable / required list -> at least REVIEW (LIST_UNAVAILABLE)
  * stale list with "hold"    -> at least REVIEW (LIST_STALE)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from swift_screening.config.models import TenantConfig
from swift_screening.decision.models import Decision
from swift_screening.decision.reason_codes import ReasonCode
from swift_screening.lists.model import ListType, WatchlistEntry
from swift_screening.matching.engine import RawHit
from swift_screening.matching.geo import GeoHit

RULE_SET_VERSION = "rules-py-1.0"


@dataclass(slots=True)
class PolicyContext:
    parse_errors: bool = False
    stale_lists: list[str] = field(default_factory=list)
    missing_lists: list[str] = field(default_factory=list)
    possible_duplicate: bool = False
    forced_reasons: list[ReasonCode] = field(default_factory=list)  # e.g. TIMEOUT, INTERNAL_ERROR


@dataclass(slots=True)
class PolicyOutcome:
    decision: Decision
    reasons: list[ReasonCode]
    hit_actions: list[Decision]
    geo_actions: list[Decision]


def hit_action(hit: RawHit, entry: WatchlistEntry, tenant: TenantConfig) -> Decision:
    if entry.list_type == ListType.PEP:
        return Decision(tenant.pep_action)
    if entry.list_type == ListType.INTERNAL:
        return Decision(tenant.internal_list_action)
    if "exact_id" in hit.methods:
        return Decision(tenant.identifier_hit_action)
    if hit.score >= tenant.thresholds.block and _blockable_name(hit, tenant):
        return Decision.BLOCK
    return Decision.REVIEW


def _blockable_name(hit: RawHit, tenant: TenantConfig) -> bool:
    """Name-only auto-BLOCK needs a long, distinctive list name. Short or common names and weak
    aliases go to REVIEW even when exact: "Mohammed Ahmed" or "ROSNAVTEK" match many people or
    things; an analyst decides with DOB / country. REVIEW still stops the payment."""
    if hit.name is None:
        return True
    distinctive = sum(1 for t in hit.name.norm.core if not t.generic and not t.initial)
    return hit.name.kind != "weak_alias" and distinctive >= tenant.thresholds.block_min_name_tokens


def evaluate(
    hits: list[RawHit],
    entries: list[WatchlistEntry],
    geo_hits: list[GeoHit],
    ctx: PolicyContext,
    tenant: TenantConfig,
) -> PolicyOutcome:
    reasons: list[ReasonCode] = []
    decision = Decision.PASS

    def add(reason: ReasonCode, at_least: Decision | None = None) -> None:
        nonlocal decision
        if reason not in reasons:
            reasons.append(reason)
        if at_least is not None:
            decision = Decision.worst(decision, at_least)

    hit_actions: list[Decision] = []
    for h in hits:
        if h.score < tenant.thresholds.review:
            hit_actions.append(Decision.PASS)
            continue
        entry = entries[h.entry_idx]
        action = hit_action(h, entry, tenant)
        hit_actions.append(action)
        if entry.list_type == ListType.PEP:
            add(ReasonCode.HIT_PEP, action)
        elif entry.list_type == ListType.INTERNAL:
            add(ReasonCode.HIT_INTERNAL, action)
        else:
            add(ReasonCode.HIT_SANCTIONS, action)
        if "exact_id" in h.methods:
            add(ReasonCode.HIT_IDENTIFIER)
        if "truncated" in h.methods:
            add(ReasonCode.TRUNCATION_SUSPECTED)

    geo_actions: list[Decision] = []
    for g in geo_hits:
        action = Decision(g.action)
        geo_actions.append(action)
        add(
            ReasonCode.EMBARGO_COUNTRY if g.kind == "country" else ReasonCode.EMBARGO_REGION, action
        )

    if ctx.parse_errors:
        add(ReasonCode.PARSE_FAILED, Decision.REVIEW)
    if ctx.missing_lists:
        add(ReasonCode.LIST_UNAVAILABLE, Decision.REVIEW)
    if ctx.stale_lists:
        add(ReasonCode.LIST_STALE, Decision.REVIEW if tenant.lists.stale_action == "hold" else None)
    for forced in ctx.forced_reasons:
        add(forced, Decision.REVIEW)
    if ctx.possible_duplicate:
        add(ReasonCode.POSSIBLE_DUPLICATE)

    if decision == Decision.PASS:
        reasons.insert(0, ReasonCode.NO_MATCH)
    return PolicyOutcome(decision, reasons, hit_actions, geo_actions)
