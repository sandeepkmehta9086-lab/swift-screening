"""Country / embargo / sanctioned-region rules.

Structured country codes (BIC chars 5-6, IBAN chars 1-2, address Ctry, 50F 3/ line, 77B residence
codes) are compared exactly. Free text is only searched for *names* of jurisdictions and regions
on token boundaries - never for 2-letter codes, which would fire on substrings ("IR" in "FIRST").
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz.distance import JaroWinkler

from swift_screening.config.models import Action, EmbargoCountry, EmbargoRegion
from swift_screening.matching.normalize import normalize_text_tokens

FUZZY_MIN_LEN = 6
FUZZY_MIN_SIM = 0.92


@dataclass(slots=True, frozen=True)
class GeoHit:
    rule_id: str  # country code or region id
    kind: str  # "country" | "region"
    action: Action
    path: str
    input_value: str
    matched: str
    method: str  # "code" | "name" | "fuzzy_name"


class GeoRules:
    def __init__(self, countries: list[EmbargoCountry], regions: list[EmbargoRegion]) -> None:
        self.by_code = {c.code: c for c in countries}
        self.phrases: list[tuple[list[str], str, str, Action]] = []
        for c in countries:
            for n in c.names:
                self._add_phrase(n, c.code, "country", c.action)
        for r in regions:
            for n in r.names:
                self._add_phrase(n, r.id, "region", r.action)

    def _add_phrase(self, name: str, rule_id: str, kind: str, action: Action) -> None:
        toks = [t.canon for t in normalize_text_tokens(name)]
        if toks:
            self.phrases.append((toks, rule_id, kind, action))

    def check_code(self, code: str, path: str) -> GeoHit | None:
        c = self.by_code.get(code.strip().upper())
        if c is None:
            return None
        return GeoHit(c.code, "country", c.action, path, code, c.code, "code")

    def check_text(self, text: str, path: str) -> list[GeoHit]:
        toks = [t.canon for t in normalize_text_tokens(text)]
        if not toks:
            return []
        hits: dict[str, GeoHit] = {}
        for phrase, rule_id, kind, action in self.phrases:
            n = len(phrase)
            for i in range(len(toks) - n + 1):
                window = toks[i : i + n]
                if window == phrase:
                    method = "name"
                elif all(
                    len(p) >= FUZZY_MIN_LEN and JaroWinkler.similarity(w, p) >= FUZZY_MIN_SIM
                    for w, p in zip(window, phrase, strict=True)
                ):
                    method = "fuzzy_name"
                else:
                    continue
                if rule_id not in hits or hits[rule_id].method != "name":
                    hits[rule_id] = GeoHit(
                        rule_id, kind, action, path, " ".join(window), " ".join(phrase), method
                    )
                break
        return list(hits.values())
