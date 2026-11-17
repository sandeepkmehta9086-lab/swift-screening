"""Deterministic screening of subjects against the watchlist index (exact IDs + fuzzy names).

This is the permanent recall layer. Later layers (semantic, ML scorer) may add or rank candidates
but must never drop a hit produced here at or above the recall floor.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from swift_screening.extraction.subjects import Subject
from swift_screening.matching.index import (
    IndexedName,
    WatchlistIndex,
    is_distinctive,
)
from swift_screening.matching.normalize import Token, normalize_name, normalize_text_tokens
from swift_screening.matching.similarity import (
    MATCH_TOKEN_SIM,
    is_whole_field_match,
    name_similarity,
    single_token_in_context_ok,
    token_similarity,
)

MAX_TEXT_TOKENS = 400
# Vessel / aircraft names ("MARIA", "GOLDEN BRIDGE") are only compared where the text talks about
# a vessel; payment parties are people and companies. Vessels are also matched by IMO / MMSI.
VESSEL_MARKER_RE = re.compile(
    r"\b(?:M/?V|M\.V\.|M/?T|VESSEL|SHIP|TANKER|CARGO|IMO|MMSI|AIRCRAFT|TAIL|FLIGHT)\b",
    re.IGNORECASE,
)
CRAFT_TYPES = {"vessel", "aircraft"}
WEAK_ALIAS_MIN_SCORE = 0.95
MAX_WINDOWS_PER_CANDIDATE = 12  # clusters per candidate


@dataclass(slots=True)
class RawHit:
    subject: Subject
    entry_idx: int
    name: IndexedName | None
    score: float
    methods: set[str]
    sources: set[str]
    matched_input: str
    identifier: str | None = None


@dataclass(slots=True)
class MatchStats:
    subjects: int = 0
    candidates_scored: int = 0
    by_kind: dict[str, int] = field(default_factory=lambda: defaultdict(int))


class Matcher:
    def __init__(self, index: WatchlistIndex, floor: float) -> None:
        self.index = index
        self.floor = floor

    # --------------------------------------------------------------------------- public
    def screen(self, subjects: list[Subject]) -> tuple[list[RawHit], MatchStats]:
        stats = MatchStats()
        hits: list[RawHit] = []
        for s in subjects:
            stats.subjects += 1
            stats.by_kind[s.kind] += 1
            if s.kind == "identifier":
                hits.extend(self._identifier(s))
            elif s.kind == "name":
                hits.extend(self._name(s, stats))
            elif s.kind == "text":
                hits.extend(self._text(s, stats))
        return _best_per_entry_and_field(hits), stats

    # --------------------------------------------------------------------------- kinds
    def _identifier(self, s: Subject) -> list[RawHit]:
        id_type = (s.id_type or "").upper()
        types = [id_type]
        if id_type in {"OTHER", "CUSTOMER_ID"}:
            types = ["PASSPORT", "NATIONAL_ID", "OTHER", "LEI", "IMO"]
        out = []
        for t in types:
            for eidx in self.index.lookup_identifier(t, s.value):
                out.append(
                    RawHit(
                        s,
                        eidx,
                        None,
                        1.0,
                        {"exact_id"},
                        {"exact"},
                        s.value,
                        identifier=f"{t}:{s.value}",
                    )
                )
        return out

    def _name(self, s: Subject, stats: MatchStats) -> list[RawHit]:
        q = normalize_name(s.value)
        if q.is_empty:
            return []
        out = []
        q_tokens = q.core
        vessel_context = bool(VESSEL_MARKER_RE.search(s.value))
        for nidx, strong in self.index.name_candidates_with_strength(q):
            iname = self.index.names[nidx]
            if not vessel_context and self.index.entries[iname.entry_idx].entry_type in CRAFT_TYPES:
                continue
            if not strong and not s.truncated and not _plausible(q_tokens, None, iname):
                continue
            stats.candidates_scored += 1
            sim = name_similarity(q, iname.norm, truncated=s.truncated)
            if iname.kind == "weak_alias" and (
                sim.score < WEAK_ALIAS_MIN_SCORE or not is_whole_field_match(q, iname.norm)
            ):
                continue  # OFAC "weak" a.k.a.: whole-field, near-exact matches only
            if sim.score >= self.floor:
                sources = {"fuzzy"}
                if "transliteration" in sim.methods:
                    sources.add("translit")
                if s.origin == "free_text_rule":
                    sources.add("free_text_rule")
                out.append(
                    RawHit(s, iname.entry_idx, iname, sim.score, sim.methods, sources, s.value)
                )
        return out

    def _text(self, s: Subject, stats: MatchStats) -> list[RawHit]:
        tokens = normalize_text_tokens(s.value)[:MAX_TEXT_TOKENS]
        if not tokens:
            return []
        positions: dict[int, set[int]] = defaultdict(set)
        strong: set[int] = set()
        weak_positions: dict[str, list[int]] = defaultdict(list)
        for p, t in enumerate(tokens):
            if t.kind in {"stop", "honorific"} or len(t.text) < 2:
                continue
            if not is_distinctive(t):  # generic/legal/particle: membership only (see below)
                weak_positions[t.canon].append(p)
                continue
            for nidx in self.index.token_postings(t):
                positions[nidx].add(p)
            if len(t.text) >= 6:  # whole name concatenated into one token ("KimCholSong")
                keys = [f"j:{t.text}", f"j:{t.canon}"]
                if len(t.text) >= 10:  # concatenation with an inner stop-word/article
                    keys += [f"p:{t.text[:5]}", f"p:{t.canon[:5]}", f"x:{t.text[-5:]}"]
                for k in keys:
                    for nidx in self.index.keys.get(k, ()):
                        positions[nidx].add(p)
                        strong.add(nidx)
        for p in range(len(tokens) - 1):  # spacing games inside free text
            joined = tokens[p].text + tokens[p + 1].text
            if len(joined) >= 3:  # exact keys only, so short pairs are safe
                # exact keys only: one-edit deletes of an arbitrary joined pair ("v"+"maria")
                # would hit single words inside unrelated multi-word names
                for k in (f"c:{joined}", f"j:{joined}"):
                    for nidx in self.index.keys.get(k, ()):
                        positions[nidx].update((p, p + 1))
                        strong.add(nidx)
        out: list[RawHit] = []
        vessel_context = bool(VESSEL_MARKER_RE.search(s.value))
        for nidx, pos in positions.items():
            iname = self.index.names[nidx]
            if iname.kind == "weak_alias":
                continue  # weak aliases never match inside free text
            if not vessel_context and self.index.entries[iname.entry_idx].entry_type in CRAFT_TYPES:
                continue
            # Distinctive hits must cover min(2, distinctive tokens of the name); generic words
            # ("trading", "ltd") are only added afterwards, to build the scoring window.
            need = min(2, max(1, iname.distinctive_tokens))
            if len(pos) < need and nidx not in strong:
                continue
            if weak_positions:
                for e in iname.norm.tokens:
                    pos.update(weak_positions.get(e.canon, ()))
            if nidx not in strong and not _plausible(tokens, pos, iname):
                continue
            best = self._best_window(tokens, sorted(pos), iname)
            stats.candidates_scored += 1
            if best is None:
                continue
            score, methods, window_text = best
            craft = self.index.entries[iname.entry_idx].entry_type in CRAFT_TYPES
            if (
                len(iname.norm.core) == 1
                and not craft  # for vessels, the vessel marker is the context evidence
                and not single_token_in_context_ok(tokens, iname.norm)
            ):
                continue  # one-word list name inside longer text must be distinctive
            if score >= self.floor:
                out.append(
                    RawHit(
                        s,
                        iname.entry_idx,
                        iname,
                        score,
                        methods,
                        {"fuzzy", "free_text_window"},
                        window_text,
                    )
                )
        return out

    def _best_window(
        self, tokens: list[Token], positions: list[int], iname: IndexedName
    ) -> tuple[float, set[str], str] | None:
        """Score a few tight windows around each cluster of hit positions."""
        size = max(1, len(iname.norm.tokens))
        clusters: list[list[int]] = []
        for p in positions:
            if clusters and p - clusters[-1][-1] <= size + 1:
                clusters[-1].append(p)
            else:
                clusters.append([p])
        best: tuple[float, set[str], str] | None = None
        tried: set[tuple[int, int]] = set()
        for cl in clusters[:MAX_WINDOWS_PER_CANDIDATE]:
            a, b = cl[0], cl[-1]
            spans = [(a, b + 1), (a - 1, b + 1), (a, b + 2), (a - 1, b + 2)]
            spans += [(p, p + 1) for p in cl]  # each hit token on its own ("M/V MARIA")
            for start, end in spans:
                start, end = max(0, start), min(len(tokens), end)
                if (start, end) in tried or end <= start:
                    continue
                tried.add((start, end))
                text = " ".join(t.text for t in tokens[start:end])
                sim = name_similarity(normalize_name(text), iname.norm)
                if best is None or sim.score > best[0]:
                    best = (sim.score, sim.methods, text)
        return best


def _plausible(tokens: list[Token], pos: set[int] | None, iname: IndexedName) -> bool:
    """Cheap pre-check mirroring the scorer's rules: >= 2 distinct entry words (or all of a
    one-word name) must have a similar token in the text, at least one of them distinctive."""
    core = iname.norm.core
    matched_words: set[int] = set()
    distinctive = False
    for e in core:
        if e.initial:
            continue
        candidates = tokens if pos is None else [tokens[p] for p in pos]
        if any(token_similarity(t, e)[0] >= MATCH_TOKEN_SIM for t in candidates):
            matched_words.add(e.word)
            distinctive = distinctive or not e.generic
    entry_words = {e.word for e in core}
    has_distinctive = any(not e.generic for e in core)
    return len(matched_words) >= min(2, len(entry_words)) and (distinctive or not has_distinctive)


def _best_per_entry_and_field(hits: list[RawHit]) -> list[RawHit]:
    best: dict[tuple[int, str], RawHit] = {}
    for h in hits:
        key = (h.entry_idx, h.subject.path.split("/line")[0].split("#")[0])
        cur = best.get(key)
        # On equal scores prefer the structured name subject: its provenance is more precise.
        if (
            cur is None
            or h.score > cur.score
            or (h.score == cur.score and h.subject.kind == "name" and cur.subject.kind != "name")
        ):
            if cur is not None:
                h.sources |= cur.sources
            best[key] = h
        else:
            cur.sources |= h.sources
    return sorted(best.values(), key=lambda h: -h.score)
