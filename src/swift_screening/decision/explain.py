"""Template explanations built only from structured evidence (no LLM in Phase 1)."""

from __future__ import annotations

from swift_screening.decision.models import Decision
from swift_screening.lists.model import WatchlistEntry
from swift_screening.matching.engine import RawHit
from swift_screening.matching.geo import GeoHit

METHOD_TEXT = {
    "exact": "exact name",
    "exact_id": "exact identifier",
    "fuzzy": "fuzzy spelling",
    "transliteration": "transliteration variant",
    "reordered": "reordered name tokens",
    "partial_tokens": "missing or extra name parts",
    "initials": "initials",
    "concatenation": "spacing/concatenation variant",
    "truncated": "truncated name",
}


def explain_hit(hit: RawHit, entry: WatchlistEntry, action: Decision, *, stateless: bool) -> str:
    where = hit.subject.path
    role = f" ({hit.subject.role})" if hit.subject.role else ""
    shown = "" if stateless else f" value '{hit.matched_input}'"
    if hit.identifier:
        basis = f"identifier {hit.identifier.split(':', 1)[0]}"
    else:
        alias = "" if hit.name is None or hit.name.kind == "primary" else f" via {hit.name.kind}"
        matched = hit.name.raw if hit.name else entry.name
        basis = f"name '{matched}'{alias}"
    methods = ", ".join(METHOD_TEXT.get(m, m) for m in sorted(hit.methods))
    return (
        f"Field {where}{role}{shown} matched {entry.list_id} entry {entry.entry_id} "
        f"('{entry.name}') on {basis} with score {hit.score:.2f} [{methods}]; action {action}."
    )


def explain_geo(g: GeoHit, *, stateless: bool) -> str:
    shown = "" if stateless else f" value '{g.input_value}'"
    what = "embargoed jurisdiction" if g.kind == "country" else "sanctioned region"
    return f"Field {g.path}{shown} references {what} {g.rule_id} ({g.method}); action {g.action}."
