"""CLI and ASGI entry point."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swift_screening.cli import main
from swift_screening.synth.generator import PartySpec, mt103, pacs008
from tests.conftest import CONFIG, FIXTURES

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(ROOT)  # fixture paths in the CLI are repo-relative
    monkeypatch.setenv("SWIFT_SCREENING_CONFIG", str(CONFIG))
    monkeypatch.setenv("SWIFT_SCREENING_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.setenv("SWIFT_SCREENING_LIST_STORE", str(tmp_path / "lists"))
    monkeypatch.setenv("SWIFT_SCREENING_LOG_JSON", "false")
    return tmp_path


def test_lists_fixtures_status_and_screen(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["lists", "load-fixtures"]) == 0
    assert "FIXTURE_SANCTIONS" in capsys.readouterr().out
    assert main(["lists", "status"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {r["list_id"] for r in rows} == {"FIXTURE_SANCTIONS", "FIXTURE_PEP", "FIXTURE_INTERNAL"}

    clean, hit, mx = env / "clean.txt", env / "hit.txt", env / "m.xml"
    clean.write_text(mt103(seed=1))
    hit.write_text(mt103(beneficiary=PartySpec(name="KIM CHOL SONG")))
    mx.write_text(pacs008(remittance="for Rosnavtek"), encoding="utf-8")
    assert main(["screen", str(clean)]) == 0  # exit code encodes the decision
    assert main(["screen", str(hit)]) == 20
    assert main(["screen", str(mx), "--mx"]) in {10, 20}
    assert main(["screen", str(clean), "--tenant", "nope"]) == 2
    capsys.readouterr()
    assert main(["audit", "verify-chain"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["rows_checked"] == 3
    assert main(["purge", "--days", "30"]) == 0


def test_lists_load_success_failure_and_shrink(env: Path) -> None:
    src = str(FIXTURES / "fixture_sanctions.json")
    args = ["lists", "load", "--format", "json", "--list-id", "X", "--list-type", "sanctions"]
    assert main([*args, "--source", src]) == 0
    assert main([*args, "--source", str(env / "missing.json")]) == 2
    tiny = env / "tiny.json"
    tiny.write_text(json.dumps({"entries": [{"entry_id": "1", "name": "Only One"}]}))
    assert main([*args, "--source", str(tiny)]) == 3  # shrink guard
    assert main([*args, "--source", str(tiny), "--force"]) == 0


def test_hash_key_and_demo(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["hash-key", "demo-key-change-me"]) == 0
    assert capsys.readouterr().out.strip().startswith("090d1548")
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "REVIEW" in out


def test_build_app_from_settings(env: Path) -> None:
    from swift_screening.api import main as asgi
    from swift_screening.settings import Settings

    assert main(["lists", "load-fixtures"]) == 0
    app = asgi.build_app(Settings())
    with TestClient(app) as c:
        r = c.post(
            "/v1/screen/mt",
            content=mt103(seed=3).encode(),
            headers={"X-API-Key": "demo-key-change-me", "Content-Type": "text/plain"},
        )
        assert r.status_code == 200 and r.json()["decision"] == "PASS"
    assert asgi.app is not None  # lazy module attribute
    with pytest.raises(AttributeError):
        _ = asgi.not_there
