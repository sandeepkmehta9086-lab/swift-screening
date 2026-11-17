"""Recall / false-positive benchmark: every list name x obfuscation x field.

Run:  swift-screening bench      (or import ``run_benchmark`` from tests)
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field

from swift_screening.config.models import TenantConfig
from swift_screening.decision.models import Decision
from swift_screening.lists.model import WatchlistEntry
from swift_screening.matching.normalize import HONORIFICS, normalize_name
from swift_screening.service import ScreeningService
from swift_screening.synth.generator import PartySpec, fake_company, fake_name, mt103, pacs008
from swift_screening.synth.obfuscate import OBFUSCATIONS

X_CHARSET = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-?:().,'+ ")


def _mt_ok(name: str) -> bool:
    return all(c in X_CHARSET for c in name)


def _fields(name: str, *, craft: bool = False) -> dict[str, str]:
    """field label -> message with ``name`` injected there.

    Vessels / aircraft are referenced in payments as cargo context ("MV <name>"), not as
    parties, and the engine only compares craft names in such context."""
    if craft:
        craft_fields = {"MX:Ustrd": pacs008(remittance=f"Freight for shipment on MV {name}")}
        if _mt_ok(name):
            craft_fields["MT:70"] = mt103(remittance=f"FREIGHT MV {name}"[:140])
        return craft_fields
    out: dict[str, str] = {
        "MX:Cdtr": pacs008(creditor=PartySpec(name=name)),
        "MX:UltmtCdtr": pacs008(ultimate_creditor=PartySpec(name=name)),
        "MX:Ustrd": pacs008(remittance=f"Invoice 4471 payment to {name} for services"),
    }
    if _mt_ok(name):
        out |= {
            "MT:59": mt103(beneficiary=PartySpec(name=name)),
            "MT:59F": mt103(beneficiary=PartySpec(name=name), beneficiary_option="F"),
            "MT:50K": mt103(ordering=PartySpec(name=name)),
            "MT:70": mt103(remittance=f"INV 4471\nPAYMENT TO {name}"[:140]),
            "MT:72": mt103(field72=f"/BNF/{name}"),
        }
    return out


FREE_TEXT_FIELDS = {"MX:Ustrd", "MT:70"}


def is_design_tradeoff(entry: WatchlistEntry, kind: str, name: str, fld: str) -> bool:
    """Misses we accept on purpose, to keep real-list false positives manageable:
    * OFAC "weak" aliases are matched on whole name fields only, never inside free text;
    * a short (< 8 letters) one-word list name inside free text is indistinguishable from
      an ordinary word ("Square", "Omar") unless the full list name is present."""
    if fld not in FREE_TEXT_FIELDS or entry.entry_type in {"vessel", "aircraft"}:
        return False
    if kind == "weak_alias":
        return True
    core = normalize_name(name).core
    return len(core) == 1 and len(core[0].text) < 8


def applicable(obf: str, entry: WatchlistEntry, name: str) -> bool:
    tokens = name.split()
    if obf in {"C.6-drop-middle"}:
        # "Abd al-Rahman X" -> "Abd X" is not a real name form ("Abd" = "servant of").
        # Same if the first word is an honorific ("Haji X" is a surname-only query).
        compound_start = tokens[0].lower() in {"abd", "abu", "bin", "ibn", "al", "el"} or (
            tokens[0].lower().strip(".") in HONORIFICS
        )
        return len(tokens) >= 3 and entry.entry_type == "individual" and not compound_start
    if obf == "C.6-add-middle":
        return len(tokens) >= 2 and entry.entry_type == "individual"
    if obf == "C.7-initial":
        return len(tokens) >= 3 and entry.entry_type == "individual"
    if obf == "C.8-honorific":
        return entry.entry_type == "individual"
    if obf in {"C.4-sub", "C.4-transpose"}:
        return max(len(t) for t in tokens) >= 5
    if obf == "C.5-reorder":
        return len(tokens) >= 2
    if obf == "C.21-concat":
        return len(tokens) >= 2
    return True


@dataclass
class BenchResult:
    total: int = 0
    hits: int = 0
    misses: list[tuple[str, str, str, str]] = field(default_factory=list)
    by_family: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    by_field: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    negatives: int = 0
    false_positives: list[str] = field(default_factory=list)
    # Misses accepted by design (reported, never silently dropped): see is_design_tradeoff.
    tradeoffs: list[tuple[str, str, str, str]] = field(default_factory=list)
    hard_negatives: int = 0
    hard_false_positives: list[str] = field(default_factory=list)

    @property
    def hard_fp_rate(self) -> float:
        return len(self.hard_false_positives) / self.hard_negatives if self.hard_negatives else 0.0

    @property
    def recall(self) -> float:
        return self.hits / self.total if self.total else 1.0

    @property
    def fp_rate(self) -> float:
        return len(self.false_positives) / self.negatives if self.negatives else 0.0

    def report(self) -> str:
        lines = [
            f"recall {self.recall:.4f} ({self.hits}/{self.total}); "
            f"false-positive rate {self.fp_rate:.4f} "
            f"({len(self.false_positives)}/{self.negatives}); hard-negative FP rate "
            f"{self.hard_fp_rate:.4f} ({len(self.hard_false_positives)}/{self.hard_negatives})"
        ]
        for fam, (h, t) in sorted(self.by_family.items()):
            lines.append(f"  {fam:22} {h:4}/{t:<4} {h / t:.3f}")
        for fld, (h, t) in sorted(self.by_field.items()):
            lines.append(f"  {fld:22} {h:4}/{t:<4} {h / t:.3f}")
        lines.append(f"  design trade-offs (excluded from recall): {len(self.tradeoffs)}")
        for m in self.tradeoffs[:10]:
            lines.append(f"  TRADEOFF {m}")
        for m in self.misses[:40]:
            lines.append(f"  MISS {m}")
        for fp in self.false_positives[:20]:
            lines.append(f"  FP   {fp}")
        for fp in self.hard_false_positives[:20]:
            lines.append(f"  HARD-FP {fp}")
        return "\n".join(lines)


def run_benchmark(
    service: ScreeningService,
    tenant: TenantConfig,
    *,
    negatives: int = 300,
    seed: int = 7,
    entry_filter: set[str] | None = None,
    primary_only: bool = False,
    fields: set[str] | None = None,
) -> BenchResult:
    res = BenchResult()
    versions, _ = service.resolve_versions(tenant)
    index = service.index_for(versions)
    for entry in index.entries:
        if entry_filter and entry.entry_id not in entry_filter:
            continue
        if entry.list_type.value != "sanctions":
            continue
        for name, kind in entry.all_names():
            if primary_only and kind != "primary":
                continue
            for obf_name, fn in {"C.1-exact": lambda n: n, **OBFUSCATIONS}.items():
                if not applicable(obf_name, entry, name):
                    continue
                variant = fn(name)
                craft = entry.entry_type in {"vessel", "aircraft"}
                for fld, msg in _fields(variant, craft=craft).items():
                    if fields is not None and fld not in fields:
                        continue
                    r = (
                        service.screen_mx(msg, tenant)
                        if fld.startswith("MX")
                        else service.screen_mt(msg, tenant)
                    )
                    ok = r.decision != Decision.PASS and any(
                        h.matched_entry.id == entry.entry_id for h in r.hits
                    )
                    res.total += 1
                    res.hits += ok
                    fam = obf_name.split("-")[0]
                    res.by_family[fam][0] += ok
                    res.by_family[fam][1] += 1
                    res.by_field[fld][0] += ok
                    res.by_field[fld][1] += 1
                    if not ok:
                        if is_design_tradeoff(entry, kind, name, fld):
                            res.tradeoffs.append((entry.entry_id, obf_name, fld, variant))
                            res.total -= 1
                            res.by_family[fam][1] -= 1
                            res.by_field[fld][1] -= 1
                        else:
                            res.misses.append((entry.entry_id, obf_name, fld, variant))
    rng = random.Random(seed)
    for i in range(negatives):
        name = fake_company(rng) if i % 3 == 0 else fake_name(rng, rng.choice([2, 3]))
        r = service.screen_mt(mt103(beneficiary=PartySpec(name=name), seed=i), tenant)
        res.negatives += 1
        if r.decision != Decision.PASS:
            res.false_positives.append(f"{name} -> {[h.matched_entry.id for h in r.hits]}")
    # Hard negatives: keep exactly one real word of a list name and replace the rest. Only names
    # with >= 2 distinctive words qualify: for "OOO GALAKTIKA" the one kept word *is* the name,
    # so flagging it is correct. Only watchlist hits count (an embargoed country named in the
    # text is a correct geo finding, not a name false positive).
    for entry in index.entries:
        if entry.list_type.value != "sanctions" or entry.entry_type in {"vessel", "aircraft"}:
            continue
        tokens = entry.name.split()
        distinctive = [t for t in normalize_name(entry.name).core if not t.generic]
        if len(tokens) < 2 or len(distinctive) < 2:
            continue
        for keep in range(len(tokens)):
            parts = [t if i == keep else fake_name(rng, 1) for i, t in enumerate(tokens)]
            name = " ".join(parts)
            r = service.screen_mt(mt103(beneficiary=PartySpec(name=name), seed=keep), tenant)
            res.hard_negatives += 1
            if r.hits:
                res.hard_false_positives.append(f"{name} -> {[h.matched_entry.id for h in r.hits]}")
    return res
