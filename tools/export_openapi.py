"""Export the OpenAPI 3.1 spec to openapi/openapi.yaml (checked in; CI fails on drift).

Usage: uv run python tools/export_openapi.py [--check]
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swift_screening.api.app import create_app  # noqa: E402
from swift_screening.config.models import load_config  # noqa: E402
from swift_screening.lists.store import ListStore  # noqa: E402
from swift_screening.service import ScreeningService  # noqa: E402

TARGET = ROOT / "openapi" / "openapi.yaml"


def build_spec() -> dict[str, Any]:
    config = load_config(ROOT / "config" / "tenants.example.yaml")
    with tempfile.TemporaryDirectory() as d:
        app = create_app(ScreeningService(ListStore(d), config), config)
        return app.openapi()


def render() -> str:
    return yaml.safe_dump(build_spec(), sort_keys=False, allow_unicode=True)


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print("openapi/openapi.yaml is out of date; run tools/export_openapi.py")
            return 1
        return 0
    TARGET.parent.mkdir(exist_ok=True)
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
