"""Registry of reason codes. Every non-PASS decision carries at least one of these."""

from __future__ import annotations

from enum import StrEnum


class ReasonCode(StrEnum):
    NO_MATCH = "NO_MATCH"
    HIT_SANCTIONS = "HIT_SANCTIONS"
    HIT_PEP = "HIT_PEP"
    HIT_INTERNAL = "HIT_INTERNAL"
    HIT_IDENTIFIER = "HIT_IDENTIFIER"
    EMBARGO_COUNTRY = "EMBARGO_COUNTRY"
    EMBARGO_REGION = "EMBARGO_REGION"
    PARSE_FAILED = "PARSE_FAILED"
    PARSE_WARNING = "PARSE_WARNING"
    LIST_STALE = "LIST_STALE"
    LIST_UNAVAILABLE = "LIST_UNAVAILABLE"
    TRUNCATION_SUSPECTED = "TRUNCATION_SUSPECTED"
    TIMEOUT = "TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"


DESCRIPTIONS: dict[ReasonCode, str] = {
    ReasonCode.NO_MATCH: "No candidate reached the review threshold on the listed list versions.",
    ReasonCode.HIT_SANCTIONS: "A party or text matched a sanctions list entry.",
    ReasonCode.HIT_PEP: "A party matched a politically exposed person entry.",
    ReasonCode.HIT_INTERNAL: "A party matched a tenant internal/private list entry.",
    ReasonCode.HIT_IDENTIFIER: (
        "An identifier (BIC, LEI, IMO, passport, ID) matched a list entry exactly."
    ),
    ReasonCode.EMBARGO_COUNTRY: "A comprehensively embargoed jurisdiction is involved.",
    ReasonCode.EMBARGO_REGION: "A sanctioned region or city is referenced.",
    ReasonCode.PARSE_FAILED: (
        "The message could not be parsed reliably; held for review (fail closed)."
    ),
    ReasonCode.PARSE_WARNING: "The message parsed with warnings.",
    ReasonCode.LIST_STALE: "At least one watchlist is older than its freshness SLA.",
    ReasonCode.LIST_UNAVAILABLE: "No usable snapshot of a required watchlist is loaded.",
    ReasonCode.TRUNCATION_SUSPECTED: "A name looks truncated by field length limits.",
    ReasonCode.TIMEOUT: "Screening exceeded its latency budget; held for review (fail closed).",
    ReasonCode.INTERNAL_ERROR: "An internal error occurred; held for review (fail closed).",
    ReasonCode.POSSIBLE_DUPLICATE: "The message is flagged as a possible duplicate emission.",
}
