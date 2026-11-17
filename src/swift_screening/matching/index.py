"""In-memory candidate index over watchlist names and identifiers (the MVP ``CandidateIndex``).

Candidate generation must be recall-safe: a true match must be *retrieved* before it can be
scored. Every name token is indexed under several keys so that each evasion family still
shares at least one key with the list name:

  c:<canon>      exact / transliterated spelling
  d:<delete-1>   one-edit typos (symmetric deletion, SymSpell style), tokens of length >= 4
  s:<skeleton>   consonant skeleton for vowel-less Arabic/Hebrew script input
  j:<joined>     whole core name without spaces (spacing / separator games)
  p:<prefix5>    containment ("Rosneftoil") and truncation
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise

from swift_screening.cpm.identifiers import bic8, normalize_bic
from swift_screening.lists.model import ListSnapshot, WatchlistEntry
from swift_screening.matching.normalize import NormalizedName, Token, normalize_name

MAX_CANDIDATES = 400


def deletes1(word: str) -> set[str]:
    return {word[:i] + word[i + 1 :] for i in range(len(word))}


def token_keys(t: Token, *, query: bool) -> set[str]:
    keys = {f"c:{t.canon}", f"c:{t.text}"}
    for form in {t.canon, t.text}:  # raw too: canon rewrites (x->ks) can add an edit
        if len(form) >= 4:
            keys |= {f"d:{d}" for d in deletes1(form)}
            keys.add(f"d:{form}")
    if len(t.canon) >= 6:
        keys.add(f"p:{t.canon[:5]}")
    if (not query or t.abjad) and len(t.skel) >= 2:
        keys.add(f"s:{t.skel}")
    return keys


def is_distinctive(t: Token) -> bool:
    return t.kind == "core" and not t.generic


def plausible_identifier(id_type: str, value: str) -> bool:
    """Defence in depth: only index values that can actually identify one party."""
    if id_type == "BIC":
        return len(value) == 8
    if id_type in {"IMO", "MMSI"}:
        return value.isdigit() and len(value) in {7, 9}
    return len(value) >= 5 and any(c.isdigit() for c in value)


def normalize_identifier(id_type: str, value: str) -> str:
    v = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    if id_type == "BIC":
        return bic8(normalize_bic(v))
    if id_type == "IMO":
        return v.removeprefix("IMO")
    return v


@dataclass(slots=True, frozen=True)
class IndexedName:
    entry_idx: int
    kind: str  # primary | alias | weak_alias
    raw: str
    norm: NormalizedName
    distinctive_tokens: int = 0  # core tokens that are not generic words


class WatchlistIndex:
    def __init__(self, snapshots: Iterable[ListSnapshot]) -> None:
        self.snapshots = list(snapshots)
        self.entries: list[WatchlistEntry] = []
        self.names: list[IndexedName] = []
        self.keys: dict[str, list[int]] = defaultdict(list)
        self.ids: dict[tuple[str, str], list[int]] = defaultdict(list)
        for snap in self.snapshots:
            for entry in snap.entries:
                self._add(entry)
        self.keys = dict(self.keys)
        self.ids = dict(self.ids)

    @property
    def versions(self) -> dict[str, str]:
        return {s.list_id: s.version for s in self.snapshots}

    def _add(self, entry: WatchlistEntry) -> None:
        eidx = len(self.entries)
        self.entries.append(entry)
        seen: set[str] = set()
        for raw, kind in entry.all_names():
            norm = normalize_name(raw)
            if norm.is_empty or norm.full_text in seen:
                continue
            seen.add(norm.full_text)
            nidx = len(self.names)
            self.names.append(
                IndexedName(eidx, kind, raw, norm, sum(1 for t in norm.core if is_distinctive(t)))
            )
            keys: set[str] = set()
            for t in norm.tokens:
                keys |= token_keys(t, query=False)
            for joined in {norm.joined_canon, norm.joined, norm.full_joined}:
                if len(joined) >= 4:
                    keys.add(f"j:{joined}")
                    keys.add(f"c:{joined}")
                if len(joined) >= 6:
                    keys.add(f"p:{joined[:5]}")
                if len(joined) >= 8:
                    keys.add(f"x:{joined[-5:]}")  # suffix: "HajiAsad...MOHAMMADHASNI"
            for t in norm.tokens:  # raw-text prefixes too (canon may differ from raw)
                if len(t.text) >= 6:
                    keys.add(f"p:{t.text[:5]}")
            for k in keys:
                self.keys[k].append(nidx)
        for ident in entry.identifiers:
            id_type = ident.type.upper()
            value = normalize_identifier(id_type, ident.value)
            if not plausible_identifier(id_type, value):
                continue  # e.g. "MALE", "2005": would exact-match far too much
            self.ids[(id_type, value)].append(eidx)

    # ------------------------------------------------------------------ lookups
    def lookup_identifier(self, id_type: str, value: str) -> list[int]:
        return self.ids.get((id_type, normalize_identifier(id_type, value)), [])

    def name_candidates(self, query: NormalizedName) -> list[int]:
        return [n for n, _ in self.name_candidates_with_strength(query)]

    def name_candidates_with_strength(self, query: NormalizedName) -> list[tuple[int, bool]]:
        """Names sharing enough keys with the query to be worth scoring.

        ``strong`` = retrieved via a whole-name / concatenation key; such candidates bypass the
        token-level plausibility pre-check (their match may not be visible token by token)."""
        hits: Counter[int] = Counter()
        strong: set[int] = set()
        q_tokens = query.tokens
        # Retrieval runs on distinctive tokens only. Generic words / legal forms / particles can
        # never make a match by themselves (the scorer requires a distinctive token), and their
        # posting lists are huge; they only add to the count of already-retrieved names.
        distinctive = [t for t in q_tokens if is_distinctive(t)] or list(q_tokens)
        weak = [t for t in q_tokens if t not in distinctive]
        for t in distinctive:
            matched: set[int] = set()
            for k in token_keys(t, query=True):
                matched.update(self.keys.get(k, ()))
            hits.update(matched)
        if weak:
            weak_canons = {t.canon for t in weak}
            for nidx, count in hits.items():
                if count < 2:
                    extra = sum(1 for e in self.names[nidx].norm.tokens if e.canon in weak_canons)
                    hits[nidx] = count + extra
        # adjacent-token concatenations ("ro sneft" -> "rosneft") and whole joined form
        pseudo = {a.text + b.text for a, b in pairwise(q_tokens)}
        pseudo |= {query.joined_canon, query.joined, query.full_joined}
        for p in pseudo:
            if len(p) >= 4:
                for k in (f"c:{p}", f"j:{p}", f"d:{p}", *(f"d:{d}" for d in deletes1(p))):
                    strong.update(self.keys.get(k, ()))
        # Prefix keys find containment ("rosnavtekoil"); they are noisy on big lists, so they
        # only count as strong when the query is one token (the concatenation case).
        prefix: set[int] = set()
        for t in q_tokens:
            if len(t.text) >= 7:
                prefix.update(self.keys.get(f"p:{t.canon[:5]}", ()))
                prefix.update(self.keys.get(f"p:{t.text[:5]}", ()))
        for joined in {query.joined_canon, query.joined, query.full_joined}:
            if len(joined) >= 6:
                prefix.update(self.keys.get(f"p:{joined[:5]}", ()))
            if len(joined) >= 10:
                prefix.update(self.keys.get(f"x:{joined[-5:]}", ()))
        # Containment is only scored when one side is a single token, so only those prefix
        # candidates can ever match.
        if len(query.core) == 1:
            strong |= prefix
        else:
            prefix = {n for n in prefix if len(self.names[n].norm.core) == 1}
        selected: list[tuple[int, int]] = []
        for nidx, count in hits.items():
            need = min(2, len(self.names[nidx].norm.core))
            if count >= need or nidx in strong or nidx in prefix:
                selected.append((count + (5 if nidx in strong else 0), nidx))
        for nidx in (strong | prefix) - set(hits):
            selected.append((5 if nidx in strong else 1, nidx))
        selected.sort(reverse=True)
        return [(n, n in strong) for _, n in selected[:MAX_CANDIDATES]]

    def token_postings(self, t: Token) -> set[int]:
        out: set[int] = set()
        for k in token_keys(t, query=True):
            out.update(self.keys.get(k, ()))
        return out
