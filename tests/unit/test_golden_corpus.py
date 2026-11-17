"""Golden corpus (Phase 0): every valid file parses cleanly; every invalid file fails closed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swift_screening.config.models import TenantConfig
from swift_screening.decision.models import Decision
from swift_screening.parsing.errors import ParseError
from swift_screening.parsing.mt.parser import parse_mt
from swift_screening.parsing.mx.parser import parse_mx
from swift_screening.service import ScreeningService

CORPUS = Path(__file__).resolve().parents[1] / "corpus"
MANIFEST: dict[str, dict[str, str]] = json.loads((CORPUS / "manifest.json").read_text())
KINDS = ["mt103", "mt202", "mt202cov", "pacs008", "pacs009"]
VALID = sorted(p for k in KINDS for p in (CORPUS / k / "valid").iterdir())
INVALID = sorted(p for k in KINDS for p in (CORPUS / k / "invalid").iterdir())


def rel(p: Path) -> str:
    return p.relative_to(CORPUS).as_posix()


def parse(p: Path):  # type: ignore[no-untyped-def]
    data = p.read_bytes()
    return parse_mt(data.decode("utf-8")) if p.suffix == ".txt" else parse_mx(data)


def test_corpus_size() -> None:
    for kind in KINDS:
        assert len(list((CORPUS / kind / "valid").iterdir())) >= 50, kind
        assert len(list((CORPUS / kind / "invalid").iterdir())) >= 50, kind
    assert set(MANIFEST) == {rel(p) for p in INVALID}


@pytest.mark.parametrize("path", VALID, ids=rel)
def test_valid_corpus_parses_cleanly(path: Path) -> None:
    cpm = parse(path)
    assert not cpm.has_parse_errors, cpm.parse_warnings
    assert cpm.parties and cpm.message_id


@pytest.mark.parametrize("path", INVALID, ids=rel)
def test_invalid_corpus_fails_with_expected_error(path: Path) -> None:
    expect = MANIFEST[rel(path)]["expect"]
    try:
        cpm = parse(path)
    except ParseError:
        assert expect == "PARSE_ERROR", f"raised ParseError, expected {expect}"
        return
    errors = {w.code for w in cpm.parse_warnings if w.severity == "error"}
    assert expect in errors, f"expected {expect}, got {errors}"


@pytest.mark.scenario("A.12")
@pytest.mark.scenario("B.9")
@pytest.mark.parametrize("path", INVALID, ids=rel)
def test_invalid_corpus_is_never_passed(
    path: Path, service: ScreeningService, tenant: TenantConfig
) -> None:
    data = path.read_bytes()
    r = (
        service.screen_mt(data.decode("utf-8"), tenant)
        if path.suffix == ".txt"
        else service.screen_mx(data, tenant)
    )
    assert r.decision != Decision.PASS
    assert "PARSE_FAILED" in r.reason_codes
