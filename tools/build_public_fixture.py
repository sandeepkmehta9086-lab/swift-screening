"""Build fixtures/lists/public_ofac_sample.json: a deterministic ~200-entry sample of the OFAC
SDN list (US Government publication, public domain) for offline tests with real-world name
distributions.

Usage: uv run python tools/build_public_fixture.py <path-or-url-to-SDN.XML> [--n 200]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swift_screening.lists.connectors.base import fetch_source  # noqa: E402
from swift_screening.lists.connectors.official import OfacSdnConnector  # noqa: E402

TARGET = ROOT / "fixtures" / "lists" / "public_ofac_sample.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?", default=OfacSdnConnector.default_url)
    ap.add_argument("--n", type=int, default=200)
    args = ap.parse_args()
    snap = OfacSdnConnector().snapshot(fetch_source(args.source, timeout=600), args.source)
    rng = random.Random(2026)
    people = [e for e in snap.entries if e.entry_type == "individual"]
    orgs = [e for e in snap.entries if e.entry_type == "organisation"]
    vessels = [e for e in snap.entries if e.entry_type == "vessel"]
    picked = (
        rng.sample(people, args.n * 45 // 100)
        + rng.sample(orgs, args.n * 45 // 100)
        + rng.sample(vessels, args.n // 10)
    )
    entries = []
    for e in sorted(picked, key=lambda x: int(x.entry_id)):
        d = e.model_dump(mode="json", exclude={"list_id", "list_type", "remarks", "attributes"})
        entries.append(d)
    doc = {
        "_source": "OFAC Specially Designated Nationals (SDN) list, "
        f"{OfacSdnConnector.default_url}, published {snap.published_at:%Y-%m-%d}",
        "_licence": "US Government work, public domain (17 U.S.C. 105). Real list entries: use "
        "only for testing screening; do not treat as current - designations change.",
        "_sampling": f"random.Random(2026); {len(entries)} entries (45% individuals, "
        "45% organisations, 10% vessels)",
        "list_id": "PUBLIC_OFAC_SAMPLE",
        "list_type": "sanctions",
        "published_at": snap.published_at.isoformat() if snap.published_at else None,
        "entries": entries,
    }
    TARGET.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {TARGET} ({len(entries)} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
