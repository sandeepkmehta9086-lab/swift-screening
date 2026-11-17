"""Deterministic name similarity (the recall layer). Pure functions, no I/O.

``name_similarity`` returns the best of several complementary views so that one evasion technique
cannot defeat all of them at once:

* token alignment  - order independent, tolerant of missing/extra middle names and initials
* joined compare   - defeats spacing / separator games ("A.B.C" vs "ABC", "Ro sneft")
* containment      - entity name embedded in a longer token ("Rosneftoil")
* prefix compare   - names truncated by MT 35-character line limits
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from itertools import pairwise

from rapidfuzz.distance import JaroWinkler
from rapidfuzz.fuzz import partial_ratio, ratio

from swift_screening.matching.normalize import NormalizedName, Token

MATCH_TOKEN_SIM = 0.80  # a token pair at/above this counts as "matched"
MIN_ENTRY_COVERAGE = 0.5  # share of a multi-word list name's weight that must match
SINGLE_TOKEN_MIN_LEN = 8  # one-word list names must be this long to match in context
STRONG_TOKEN_SIM = 0.86


@dataclass(slots=True)
class Similarity:
    score: float
    methods: set[str] = field(default_factory=set)
    matched_tokens: int = 0


@lru_cache(maxsize=500_000)
def token_similarity(q: Token, e: Token) -> tuple[float, str]:
    if q.text == e.text:
        return 1.0, "exact"
    if q.initial or e.initial:
        if q.initial and e.initial:
            return 0.0, ""
        full, init = (e, q) if q.initial else (q, e)
        return (0.88, "initials") if full.text[0] == init.text else (0.0, "")
    if q.canon == e.canon:
        return 0.97, "transliteration"
    sim = max(JaroWinkler.similarity(q.canon, e.canon), JaroWinkler.similarity(q.text, e.text))
    method = "fuzzy"
    if (q.abjad or e.abjad) and q.skel and e.skel:
        if q.skel == e.skel and len(q.skel) >= 2:
            return max(sim, 0.95), "transliteration"
        skel_sim = JaroWinkler.similarity(q.skel, e.skel) * 0.93
        if skel_sim > sim:
            sim, method = skel_sim, "transliteration"
    if min(len(q.canon), len(e.canon)) <= 3:
        sim *= 0.75  # short tokens: Jaro-Winkler is too generous
    return sim, method


GENERIC_WEIGHT = 0.35


def _weight(t: Token) -> float:
    if t.initial:
        return 0.5
    if t.generic:
        return GENERIC_WEIGHT
    return max(0.5, min(len(t.text), 8) / 8)


def align(q_tokens: list[Token], e_tokens: list[Token]) -> Similarity:
    """Greedy best-pair alignment of query tokens to entry tokens (order independent)."""
    if not q_tokens or not e_tokens:
        return Similarity(0.0)
    pairs: list[tuple[float, int, int, str]] = []
    for i, q in enumerate(q_tokens):
        for j, e in enumerate(e_tokens):
            s, m = token_similarity(q, e)
            if s >= MATCH_TOKEN_SIM:
                pairs.append((s, i, j, m))
    pairs.sort(key=lambda p: -p[0])
    used_q: set[int] = set()
    used_e: dict[int, float] = {}
    methods: set[str] = set()
    order: list[tuple[int, int]] = []
    for s, i, j, m in pairs:
        if i in used_q or j in used_e:
            continue
        used_q.add(i)
        used_e[j] = s
        order.append((i, j))
        if m and m != "exact":
            methods.add(m)
    if not used_e:
        return Similarity(0.0)
    w_all = sum(_weight(e) for e in e_tokens)
    w_matched = sum(_weight(e_tokens[j]) for j in used_e)
    quality = sum(s * _weight(e_tokens[j]) for j, s in used_e.items()) / w_matched
    cov_e = w_matched / w_all
    cov_q = len(used_q) / len(q_tokens)
    score = quality * (0.70 + 0.30 * cov_e) * (0.88 + 0.12 * cov_q)
    # First + last word matched is the normal short form of a person's name ("Jose Contreras"
    # for "Jose de Jesus Amezcua Contreras"); otherwise under half of a long name is no match.
    first_last = (
        0 in used_e
        and len(e_tokens) - 1 in used_e
        and not e_tokens[0].generic
        and not e_tokens[-1].generic  # people's names, not "Golden ... Trading"
    )
    if len(e_tokens) >= 2 and cov_e < MIN_ENTRY_COVERAGE and not first_last:
        score = min(score, 0.75)  # under half of a long name is no match
    strong_idx = [j for j, s in used_e.items() if s >= STRONG_TOKEN_SIM and not e_tokens[j].initial]
    # Count distinct source *words*: "Muller-Brandt" is one surname, not two matches.
    strong_words = {e_tokens[j].word for j in strong_idx}
    matched_words = {e_tokens[j].word for j in used_e}
    entry_words = {e.word for e in e_tokens}
    if (
        len(entry_words) >= 2
        and len(strong_words) < 2
        and not (len(strong_words) == 1 and len(matched_words) >= 2)
    ):
        score = min(score, 0.65)  # one matching surname is not a name match
    distinctive = [e for e in e_tokens if not e.generic]
    if distinctive and not any(not e_tokens[j].generic for j in strong_idx):
        score = min(score, 0.65)  # only generic words ("general trading") matched
    # A 2-3 letter token ("SG" country code, "AO") cannot be the only distinctive evidence,
    # unless every distinctive word of the name is that short ("Li Wei", "Kim Ho").
    all_short = all(len(e.text) < 4 for e in e_tokens)
    if (
        len(e_tokens) >= 2
        and not all_short
        and not any(not e_tokens[j].generic and len(e_tokens[j].text) >= 4 for j in strong_idx)
    ):
        score = min(score, 0.65)
    if len(e_tokens) == 1 and len(e_tokens[0].text) < 4 and score < 0.999:
        score = min(score, 0.70)  # fuzzy matches of 2-3 letter names are noise; exact ones count
    # First name + final surname both matched strongly: the usual short form of a person's name
    # ("Ivan Meza" for "Ivan Atzayacatl Castaneda Meza") - always worth a review.
    if (
        first_last
        and len(e_tokens) >= 3
        and used_e.get(0, 0) >= STRONG_TOKEN_SIM
        and used_e.get(len(e_tokens) - 1, 0) >= STRONG_TOKEN_SIM
        and not e_tokens[0].initial
        and score > 0.65
    ):
        score = max(score, quality * 0.86)
    ordered = [j for _, j in sorted(order)]
    if ordered != sorted(ordered):
        methods.add("reordered")
    if len(used_e) < len(e_tokens) or len(used_q) < len(q_tokens):
        methods.add("partial_tokens")
    if not methods:
        methods = {"exact"} if score >= 0.999 else {"fuzzy"}
    return Similarity(score, methods, matched_tokens=len(used_e))


def _joined_views(context: list[Token]) -> list[str]:
    """Context tokens plus adjacent pairs/triples joined ("Ros navtek" -> "rosnavtek")."""
    out = [t.text for t in context]
    out += [a.text + b.text for a, b in pairwise(context)]
    out += [
        a.text + b.text + c.text for a, b, c in zip(context, context[1:], context[2:], strict=False)
    ]
    return out


def is_whole_field_match(q: NormalizedName, e: NormalizedName) -> bool:
    """The query *is* the list name, possibly re-spaced ("Om ar" for "Omar")."""
    for qj, ej in (
        (q.joined, e.joined),
        (q.full_joined, e.full_joined),
        (q.joined_canon, e.joined_canon),
    ):
        if qj and ej and (qj == ej or (min(len(qj), len(ej)) >= 6 and ratio(qj, ej) >= 90)):
            return True
    return False


def single_token_in_context_ok(context: list[Token], e: NormalizedName) -> bool:
    """A one-word list name found *inside* a longer name or free text is weak evidence
    ("Square" in "Market Square", "John" in "John Smith"). Accept it only if
      * the full list name is present: its core word exactly plus every other word (legal
        form, "Group") at least fuzzily, or its whole joined form ("ZIHGroup", "ZIH Gr oup"); or
      * the word is distinctive: >= 8 letters and contained in, or near-exact to, a token."""
    word = e.core[0]
    ctx = {t.canon for t in context}
    if len(e.tokens) > 1 and word.canon in ctx:
        others = [t for t in e.tokens if t is not word]
        if all(
            t.canon in ctx or any(token_similarity(c, t)[0] >= MATCH_TOKEN_SIM for c in context)
            for t in others
        ):
            return True
    views = _joined_views(context)
    if len(e.tokens) > 1 and len(e.full_joined) >= 6 and any(e.full_joined in v for v in views):
        return True
    if len(word.text) < SINGLE_TOKEN_MIN_LEN:
        return False
    if any(word.text in v or word.canon in v for v in views):
        return True
    for t in context:
        sim, _ = token_similarity(t, word)
        if sim >= 0.90 and abs(len(t.text) - len(word.text)) <= 1:
            return True
    return False


def name_similarity(q: NormalizedName, e: NormalizedName, *, truncated: bool = False) -> Similarity:
    if q.is_empty or e.is_empty:
        return Similarity(0.0)
    sim = _name_similarity(q, e, truncated=truncated)
    distinctive_q = [t for t in q.core if not t.generic and not t.initial]
    if (
        len(e.core) == 1
        and len(distinctive_q) > 1
        and not is_whole_field_match(q, e)
        and not single_token_in_context_ok(q.tokens, e)
    ):
        return Similarity(min(sim.score, 0.70), sim.methods, sim.matched_tokens)
    return sim


def _name_similarity(q: NormalizedName, e: NormalizedName, *, truncated: bool) -> Similarity:
    best = align(q.core, e.core)
    # Arabic particles / legal forms matter when the core is tiny (e.g. "Al Qaida")
    if len(e.core) <= 1 and len(e.tokens) > len(e.core):
        full = align(q.tokens, e.tokens)
        if full.score > best.score:
            best = full

    token_shape_differs = len(q.core) != len(e.core)
    # Containment only models concatenation ("Rosnavtekoil", "ZarkhovIndustrial"): one side
    # must be a single token, otherwise a shared surname would look like containment.
    allow_containment = len(e.core) == 1 or len(q.core) == 1
    # Joined (space-free) views: canonical core, raw core, raw incl. particles/legal forms.
    pairs = {(q.joined_canon, e.joined_canon), (q.joined, e.joined), (q.full_joined, e.full_joined)}
    for qj, ej in pairs:
        if len(qj) < 6 or len(ej) < 6:
            if len(qj) >= 3 and qj == ej and best.score < 1.0:  # short re-spaced name "Ma ria"
                best = Similarity(1.0, {"concatenation"}, best.matched_tokens)
            continue
        r = ratio(qj, ej) / 100
        methods = {"concatenation"} if r >= 0.999 and token_shape_differs else {"fuzzy"}
        joined_score = r if r >= 0.999 else r * 0.97
        if joined_score > best.score:
            best = Similarity(joined_score, methods, best.matched_tokens)
        # Entity name embedded inside a longer string ("rosneftoil", "xabctradingx").
        if allow_containment and len(qj) > len(ej) and len(ej) / len(qj) >= 0.5:
            pr = partial_ratio(ej, qj) / 100
            if pr * 0.93 > best.score:
                best = Similarity(pr * 0.93, {"concatenation"}, best.matched_tokens)
        # Truncated name: query is a prefix of the list name.
        if (truncated or len(qj) >= 20) and len(qj) < len(ej):
            pre = ratio(qj, ej[: len(qj)]) / 100
            factor = 0.97 if truncated else 0.93
            if pre * factor > best.score:
                best = Similarity(pre * factor, {"truncated"}, best.matched_tokens)
    return Similarity(round(min(best.score, 1.0), 4), best.methods, best.matched_tokens)
