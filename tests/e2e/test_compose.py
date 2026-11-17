"""End-to-end tests against a running deployment (docker compose: API + PostgreSQL).

Run:  make e2e        (builds, starts, tests, stops)
  or: SWIFT_SCREENING_E2E_URL=http://127.0.0.1:8080 uv run pytest tests/e2e -v
Skipped unless SWIFT_SCREENING_E2E_URL is set.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from swift_screening.synth.generator import PartySpec, mt103, pacs008

URL = os.environ.get("SWIFT_SCREENING_E2E_URL")
KEY = os.environ.get("SWIFT_SCREENING_E2E_KEY", "demo-key-change-me")
pytestmark = pytest.mark.skipif(not URL, reason="set SWIFT_SCREENING_E2E_URL to run e2e tests")
MT = {"X-API-Key": KEY, "Content-Type": "text/plain"}
MX = {"X-API-Key": KEY, "Content-Type": "application/xml"}


@pytest.fixture(scope="module")
def http() -> httpx.Client:
    assert URL
    with httpx.Client(base_url=URL, timeout=30) as c:
        yield c


def test_health_and_ready(http: httpx.Client) -> None:
    assert http.get("/healthz").json() == {"status": "ok"}
    ready = http.get("/readyz").json()
    assert ready["ready"] is True and ready["database"] is True
    assert all(ready["lists_loaded"].values())


def test_lists_status(http: httpx.Client) -> None:
    rows = http.get("/v1/lists/status", headers={"X-API-Key": KEY}).json()
    assert {r["list_id"] for r in rows} == {"FIXTURE_SANCTIONS", "FIXTURE_PEP", "FIXTURE_INTERNAL"}
    assert all(not r["stale"] and r["entries"] > 0 for r in rows)


def test_clean_mt_passes_and_is_persisted(http: httpx.Client) -> None:
    r = http.post("/v1/screen/mt", content=mt103(seed=101), headers=MT).json()
    assert r["decision"] == "PASS" and r["reason_codes"] == ["NO_MATCH"]
    stored = http.get(f"/v1/results/{r['screening_id']}", headers={"X-API-Key": KEY}).json()
    assert stored["screening_id"] == r["screening_id"] and stored["decision"] == "PASS"


def test_sanctions_hit_blocks(http: httpx.Client) -> None:
    msg = mt103(beneficiary=PartySpec(name="VLADIMIR SERGEYEVICH KORNILOV"), seed=102)
    r = http.post("/v1/screen/mt", content=msg, headers=MT).json()
    assert r["decision"] == "BLOCK"
    assert r["hits"][0]["matched_entry"]["id"] == "FX-0002"
    assert r["hits"][0]["field"] == "MT:59"


def test_mx_hidden_party_and_region(http: httpx.Client) -> None:
    xml = pacs008(
        creditor=PartySpec(name="Harbour Supplies", town="SEVASTOPOL", country="UA"),
        remittance="Invoice 7 on behalf of Al Noor Exchange",
        seed=103,
    )
    r = http.post("/v1/screen/mx", content=xml.encode(), headers=MX).json()
    assert r["decision"] != "PASS"
    assert "EMBARGO_REGION" in r["reason_codes"]
    assert "FX-1009" in {h["matched_entry"]["id"] for h in r["hits"]}


def test_entity_and_identifier(http: httpx.Client) -> None:
    r = http.post(
        "/v1/screen/entity",
        headers={"X-API-Key": KEY},
        json={"name": "Someone", "identifiers": [{"type": "BIC", "value": "ZYXBIRTH"}]},
    )
    assert r.status_code == 200 and r.json()["decision"] == "BLOCK"


def test_unparseable_fails_closed(http: httpx.Client) -> None:
    r = http.post("/v1/screen/mt", content=b"not a swift message", headers=MT).json()
    assert r["decision"] == "REVIEW" and "PARSE_FAILED" in r["reason_codes"]
    x = http.post(
        "/v1/screen/mx", content=b'<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>', headers=MX
    ).json()
    assert x["decision"] == "REVIEW" and "PARSE_FAILED" in x["reason_codes"]


def test_idempotent_resubmission(http: httpx.Client) -> None:
    msg = mt103(seed=104)
    a = http.post("/v1/screen/mt", content=msg, headers=MT).json()
    b = http.post("/v1/screen/mt", content=msg, headers=MT).json()
    assert a["screening_id"] == b["screening_id"] and b["idempotent_replay"] is True


@pytest.mark.parametrize(
    "headers,body,status",
    [
        ({"Content-Type": "text/plain"}, b":20:X", 401),
        ({"X-API-Key": "wrong", "Content-Type": "text/plain"}, b":20:X", 401),
        ({"X-API-Key": KEY, "Content-Type": "application/json"}, b"{}", 415),
        ({"X-API-Key": KEY, "Content-Type": "text/plain"}, b"x" * 1_000_001, 413),
        ({"X-API-Key": KEY, "Content-Type": "text/plain"}, b"   ", 422),
    ],
)
def test_request_validation(
    http: httpx.Client, headers: dict[str, str], body: bytes, status: int
) -> None:
    assert http.post("/v1/screen/mt", content=body, headers=headers).status_code == status


def test_unknown_result_is_404(http: httpx.Client) -> None:
    assert http.get("/v1/results/scr_nope", headers={"X-API-Key": KEY}).status_code == 404


def test_concurrent_requests_keep_postgres_audit_chain_consistent(http: httpx.Client) -> None:
    """Exercises the PostgreSQL SELECT ... FOR UPDATE path of the audit chain."""
    msgs = [
        mt103(
            seed=2000 + i,
            beneficiary=PartySpec(name="KIM CHOL SONG" if i % 5 == 0 else f"Person Number{i}"),
        )
        for i in range(60)
    ]

    def send(m: str) -> str:
        return str(http.post("/v1/screen/mt", content=m, headers=MT).json()["decision"])

    with ThreadPoolExecutor(max_workers=12) as pool:
        decisions = list(pool.map(send, msgs))
    assert decisions.count("BLOCK") == 12
    # chain verification runs via the CLI inside the container (see Makefile e2e target)


def test_metrics_exposed(http: httpx.Client) -> None:
    text = http.get("/metrics").text
    assert "swift_screening_decisions_total" in text
    assert 'decision="BLOCK"' in text
