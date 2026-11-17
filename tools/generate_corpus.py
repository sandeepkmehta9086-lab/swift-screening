"""Generate the golden corpus under tests/corpus/ (deterministic; synthetic data only).

For each message type: 50 valid messages and 50 invalid ones (10 mutation kinds x 5 seeds).
tests/corpus/manifest.json records, per invalid file, the mutation and the expected error.

Usage: uv run python tools/generate_corpus.py
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swift_screening.synth.generator import (  # noqa: E402
    mt103,
    mt202,
    mt202cov,
    pacs008,
    pacs009,
    random_party,
)

CORPUS = ROOT / "tests" / "corpus"
SEEDS = range(5)
VALID_PER_TYPE = 50

Mutation = Callable[[str], str]


def _drop_field(tag: str) -> Mutation:
    return lambda m: re.sub(rf"\n:{tag}:[^\n]*(?:\n(?!:|-\}})[^\n]*)*", "", m, count=1)


MT_MUTATIONS: dict[str, tuple[Mutation, str]] = {
    "missing-20": (_drop_field("20"), "MISSING_MANDATORY_FIELD"),
    "missing-32A": (_drop_field("32A"), "MISSING_MANDATORY_FIELD"),
    "bad-32A": (lambda m: re.sub(r":32A:\d{6}", ":32A:2611", m, count=1), "INVALID_32A"),
    # first option-A party field present in the message type (52A in MT103, 58A in MT202...)
    "bad-option": (
        lambda m: re.sub(r"\n:(5[2-9])A:", r"\n:\g<1>Z:", m, count=1),
        "INVALID_OPTION",
    ),
    "bad-bic": (
        lambda m: re.sub(r"\n:(5[2-9])A:[A-Z0-9]+", r"\n:\g<1>A:NOT-A-BIC", m, count=1),
        "INVALID_BIC",
    ),
    "bad-block1": (lambda m: m.replace("{1:F01", "{1:F0", 1), "INVALID_BLOCK1"),
    "bad-block2": (lambda m: re.sub(r"\{2:I\d{3}", "{2:X", m, count=1), "INVALID_BLOCK2"),
    "no-headers": (lambda m: m[m.index("{4:") + 3 : m.index("\n-}")], "MISSING_HEADER_BLOCKS"),
    "unterminated-block4": (lambda m: m.replace("\n-}", "\n", 1), "PARSE_ERROR"),
    "garbage": (lambda m: "THIS IS NOT A SWIFT MESSAGE " + m[:20], "PARSE_ERROR"),
}

MX_MUTATIONS: dict[str, tuple[Mutation, str]] = {
    "drop-dbtr": (
        lambda m: re.sub(r"<Dbtr>.*?</Dbtr>", "", m, count=1, flags=re.S),
        "XSD_STRUCTURE",
    ),
    "drop-cdtr": (
        lambda m: re.sub(r"<Cdtr>.*?</Cdtr>", "", m, count=1, flags=re.S),
        "XSD_STRUCTURE",
    ),
    "drop-amount": (
        lambda m: re.sub(r"<IntrBkSttlmAmt[^>]*>[^<]*</IntrBkSttlmAmt>", "", m, count=1),
        "XSD_STRUCTURE",
    ),
    "bad-currency": (
        lambda m: re.sub(r'Ccy="[A-Z]{3}"', 'Ccy="EURO"', m, count=1),
        "XSD_STRUCTURE",
    ),
    "bad-amount": (
        lambda m: re.sub(r"(<IntrBkSttlmAmt[^>]*>)[^<]*", r"\g<1>12,5", m, count=1),
        "XSD_STRUCTURE",
    ),
    "no-msgid": (lambda m: re.sub(r"<MsgId>[^<]*</MsgId>", "", m, count=1), "XSD_STRUCTURE"),
    "bad-bic": (
        lambda m: re.sub(r"<BICFI>[^<]*</BICFI>", "<BICFI>NOT-A-BIC</BICFI>", m, count=1),
        "INVALID_BIC",
    ),
    "doctype": (lambda m: m.replace("?>", '?><!DOCTYPE d [<!ENTITY x "y">]>', 1), "PARSE_ERROR"),
    "malformed": (lambda m: m[: len(m) // 2], "PARSE_ERROR"),
    "wrong-namespace": (
        lambda m: re.sub(
            # the Document namespace, not the AppHdr (head.001) one
            r"urn:iso:std:iso:20022:tech:xsd:(?:pacs|pain|camt)[a-z.0-9]+",
            "urn:example:not-iso",
            m,
            count=1,
        ),
        "PARSE_ERROR",
    ),
}


def valid_messages(kind: str) -> list[str]:
    import random

    out = []
    for i in range(VALID_PER_TYPE):
        rng = random.Random(1000 + i)
        a, b = random_party(rng), random_party(rng, company=i % 2 == 0)
        if kind == "mt103":
            out.append(
                mt103(
                    ordering=a,
                    beneficiary=b,
                    seed=i,
                    remittance=f"INVOICE {i}",
                    ordering_option="F" if i % 3 == 0 else "K",
                    beneficiary_option="F" if i % 4 == 0 else "",
                )
            )
        elif kind == "mt202":
            out.append(mt202(intermediary="INGBNL2AXXX" if i % 2 else None))
        elif kind == "mt202cov":
            out.append(mt202cov(ordering=a, beneficiary=b, remittance=f"COVER {i}"))
        elif kind == "pacs008":
            out.append(
                pacs008(
                    debtor=a,
                    creditor=b,
                    seed=i,
                    transactions=1 + i % 3,
                    remittance=f"Invoice {i}",
                    structured_address=i % 2 == 0,
                    with_app_hdr=i % 5 == 0,
                )
            )
        elif kind == "pacs009":
            cov = i % 2 == 0
            out.append(pacs009(cov_debtor=a if cov else None, cov_creditor=b if cov else None))
    return out


def main() -> int:
    manifest: dict[str, dict[str, str]] = {}
    for kind in ("mt103", "mt202", "mt202cov", "pacs008", "pacs009"):
        ext = "txt" if kind.startswith("mt") else "xml"
        mutations = MT_MUTATIONS if kind.startswith("mt") else MX_MUTATIONS
        valid = valid_messages(kind)
        vdir, idir = CORPUS / kind / "valid", CORPUS / kind / "invalid"
        for d in (vdir, idir):
            d.mkdir(parents=True, exist_ok=True)
            for old in d.glob(f"*.{ext}"):
                old.unlink()
        for i, msg in enumerate(valid):
            (vdir / f"{i:03d}.{ext}").write_text(msg, encoding="utf-8", newline="\n")
        n = 0
        for name, (mutate, expected) in mutations.items():
            for seed in SEEDS:
                original = valid[seed * 7 % len(valid)]
                broken = mutate(original)
                if broken == original:  # a no-op mutation would "prove" nothing
                    raise SystemExit(f"mutation {name} did not change a {kind} message")
                fname = f"{n:03d}-{name}.{ext}"
                (idir / fname).write_text(broken, encoding="utf-8", newline="\n")
                manifest[f"{kind}/invalid/{fname}"] = {"mutation": name, "expect": expected}
                n += 1
    (CORPUS / "manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote corpus to {CORPUS} ({len(manifest)} invalid files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
