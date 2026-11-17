"""REST API behaviour H.1 - H.8 and endpoint contracts."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from swift_screening.api.app import create_app
from swift_screening.config.models import AppConfig
from swift_screening.service import ScreeningService
from swift_screening.synth.generator import PartySpec, mt103, pacs008
from tests.conftest import OTHER_KEY, PUBLIC_KEY, TEST_KEY, auth

MT_HEADERS = {**auth(), "Content-Type": "text/plain"}
MX_HEADERS = {**auth(), "Content-Type": "application/xml"}


def post_mt(client: TestClient, body: str, key: str = TEST_KEY):  # type: ignore[no-untyped-def]
    return client.post(
        "/v1/screen/mt", content=body.encode(), headers={**auth(key), "Content-Type": "text/plain"}
    )


def test_screen_mt_pass(client: TestClient) -> None:
    r = post_mt(client, mt103(seed=1))
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "PASS" and body["reason_codes"] == ["NO_MATCH"]
    assert body["screening_id"].startswith("scr_")
    assert set(body["list_versions"]) == {"FIXTURE_SANCTIONS", "FIXTURE_PEP", "FIXTURE_INTERNAL"}
    for key in (
        "config_version",
        "rule_set_version",
        "engine_version",
        "input_hash",
        "model_version",
        "latency_ms",
    ):
        assert body[key], key
    assert r.headers["cache-control"] == "no-store"


def test_screen_mt_hit_contract(client: TestClient) -> None:
    r = post_mt(client, mt103(beneficiary=PartySpec(name="KIM CHOL SONG")))
    body = r.json()
    assert body["decision"] == "BLOCK"
    hit = body["hits"][0]
    assert hit["matched_entry"]["list"] == "FIXTURE_SANCTIONS"
    assert hit["matched_entry"]["id"] == "FX-0007"
    assert hit["field"] == "MT:59" and hit["input_value"] == "KIM CHOL SONG"
    assert hit["score"] == 1.0 and hit["recall_floor_protected"] is True
    assert hit["explanation"]


def test_screen_mx(client: TestClient) -> None:
    r = client.post(
        "/v1/screen/mx", content=pacs008(remittance="for Rosnavtek").encode(), headers=MX_HEADERS
    )
    assert r.status_code == 200 and r.json()["decision"] != "PASS"


@pytest.mark.scenario("H.1")
@pytest.mark.parametrize(
    "headers",
    [{}, {"X-API-Key": "wrong"}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}],
)
def test_auth_required(client: TestClient, headers: dict[str, str]) -> None:
    r = client.post(
        "/v1/screen/mt", content=b":20:X", headers={**headers, "Content-Type": "text/plain"}
    )
    assert r.status_code == 401


def test_bearer_token_accepted(client: TestClient) -> None:
    r = client.post(
        "/v1/screen/mt",
        content=mt103(seed=3).encode(),
        headers={"Authorization": f"Bearer {TEST_KEY}", "Content-Type": "text/plain"},
    )
    assert r.status_code == 200


@pytest.mark.scenario("H.2")
@pytest.mark.scenario("O.4")
def test_rate_limit(client: TestClient) -> None:
    codes = [post_mt(client, mt103(seed=i), key=PUBLIC_KEY).status_code for i in range(7)]
    assert codes[:5] == [200] * 5 and codes[-1] == 429
    r = post_mt(client, mt103(seed=1), key=PUBLIC_KEY)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


@pytest.mark.scenario("H.3")
@pytest.mark.parametrize(
    "path,ctype",
    [
        ("/v1/screen/mt", "application/json"),
        ("/v1/screen/mx", "text/plain"),
        ("/v1/screen/entity", "text/plain"),
    ],
)
def test_wrong_content_type(client: TestClient, path: str, ctype: str) -> None:
    r = client.post(path, content=b"x", headers={**auth(), "Content-Type": ctype})
    assert r.status_code == 415


@pytest.mark.scenario("H.4")
def test_payload_too_large(client: TestClient) -> None:
    r = client.post("/v1/screen/mt", content=b"x" * 1_000_001, headers=MT_HEADERS)
    assert r.status_code == 413


def test_empty_body_422(client: TestClient) -> None:
    assert client.post("/v1/screen/mt", content=b"  ", headers=MT_HEADERS).status_code == 422


@pytest.mark.scenario("H.5")
def test_entity_screening(client: TestClient) -> None:
    r = client.post(
        "/v1/screen/entity",
        headers=auth(),
        json={
            "name": "Natalia Vorontsova",
            "country": "RU",
            "date_of_birth": "1980-01-01",
            "entity_type": "individual",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "REVIEW" and body["hits"][0]["matched_entry"]["id"] == "FX-0010"
    r2 = client.post(
        "/v1/screen/entity",
        headers=auth(),
        json={"name": "Somebody Else", "identifiers": [{"type": "BIC", "value": "ZYXBIRTH"}]},
    )
    assert r2.json()["decision"] == "BLOCK"


@pytest.mark.parametrize(
    "payload", [{}, {"name": ""}, {"name": "x" * 501}, {"name": "a", "country": "TOOLONG"}]
)
def test_entity_validation(client: TestClient, payload: dict[str, object]) -> None:
    assert client.post("/v1/screen/entity", headers=auth(), json=payload).status_code == 422


@pytest.mark.scenario("H.6")
def test_results_are_tenant_isolated(client: TestClient) -> None:
    sid = post_mt(client, mt103(seed=9)).json()["screening_id"]
    own = client.get(f"/v1/results/{sid}", headers=auth())
    assert own.status_code == 200 and own.json()["screening_id"] == sid
    assert client.get(f"/v1/results/{sid}", headers=auth(OTHER_KEY)).status_code == 404
    assert client.get("/v1/results/scr_doesnotexist", headers=auth()).status_code == 404


@pytest.mark.scenario("H.7")
def test_idempotent_resubmission(client: TestClient) -> None:
    msg = mt103(beneficiary=PartySpec(name="KIM CHOL SONG"), seed=11)
    a = post_mt(client, msg).json()
    b = post_mt(client, msg).json()
    assert a["screening_id"] == b["screening_id"]
    assert b["idempotent_replay"] is True and a["idempotent_replay"] is False
    # a different tenant screening the same message gets its own result
    c = post_mt(client, msg, key=OTHER_KEY).json()
    assert c["screening_id"] != a["screening_id"]


@pytest.mark.scenario("H.8")
def test_internal_error_returns_review_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import swift_screening.service as svc_mod

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(svc_mod.Matcher, "screen", boom)
    r = post_mt(client, mt103(seed=21))
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "REVIEW" and body["reason_codes"] == ["INTERNAL_ERROR"]
    # the fail-closed result is persisted and auditable
    assert client.get(f"/v1/results/{body['screening_id']}", headers=auth()).status_code == 200


@pytest.mark.scenario("L.8")
def test_timeout_fails_closed(
    service_db: ScreeningService, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import swift_screening.service as svc_mod

    real = svc_mod.Matcher.screen

    def slow(self, subjects):  # type: ignore[no-untyped-def]
        time.sleep(0.5)
        return real(self, subjects)

    t1 = config.tenant("t1")
    assert t1 is not None
    fast_cfg = AppConfig(
        tenants=[
            t1.model_copy(update={"screening_timeout_ms": 100}),
            *[t for t in config.tenants if t.tenant_id != "t1"],
        ]
    )
    service_db.config = fast_cfg
    monkeypatch.setattr(svc_mod.Matcher, "screen", slow)
    with TestClient(create_app(service_db, fast_cfg)) as c:
        r = post_mt(c, mt103(seed=31))
    assert r.status_code == 200
    assert r.json()["decision"] == "REVIEW" and r.json()["reason_codes"] == ["TIMEOUT"]


@pytest.mark.scenario("L.1")
def test_database_down_returns_503_never_pass(
    service_db: ScreeningService, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from swift_screening.storage.db import StorageUnavailable

    def down(*a: object, **k: object) -> None:
        raise StorageUnavailable("connection refused")

    assert service_db.db is not None
    monkeypatch.setattr(service_db.db, "find_by_idempotency", down)
    with TestClient(create_app(service_db, config)) as c:
        r = post_mt(c, mt103(seed=41))
    assert r.status_code == 503 and "retry-after" in r.headers


@pytest.mark.scenario("O.1")
def test_public_mode_hides_scores_and_values(client: TestClient) -> None:
    body = post_mt(
        client, mt103(beneficiary=PartySpec(name="KIM CHOL SONG")), key=PUBLIC_KEY
    ).json()
    assert body["decision"] == "BLOCK" and body["risk_score"] is None
    assert body["stateless"] is True
    for h in body["hits"]:
        assert h["score"] is None and h["input_value"] is None
        assert "KIM CHOL SONG" not in h["explanation"]


def test_unparseable_message_via_api(client: TestClient) -> None:
    body = post_mt(client, "this is definitely not a swift message").json()
    assert body["decision"] == "REVIEW" and "PARSE_FAILED" in body["reason_codes"]
    xml = client.post("/v1/screen/mx", content=b"<not-closed>", headers=MX_HEADERS).json()
    assert xml["decision"] == "REVIEW" and "PARSE_FAILED" in xml["reason_codes"]


def test_lists_status(client: TestClient) -> None:
    r = client.get("/v1/lists/status", headers=auth())
    assert r.status_code == 200
    rows = {row["list_id"]: row for row in r.json()}
    assert rows["FIXTURE_SANCTIONS"]["entries"] == 23 and not rows["FIXTURE_SANCTIONS"]["stale"]


def test_health_ready_metrics_openapi(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["ready"] is True
    post_mt(client, mt103(seed=2))
    metrics = client.get("/metrics").text
    assert "swift_screening_decisions_total" in metrics
    spec = client.get("/openapi.json").json()
    for path in (
        "/v1/screen/mt",
        "/v1/screen/mx",
        "/v1/screen/entity",
        "/v1/results/{screening_id}",
        "/v1/lists/status",
    ):
        assert path in spec["paths"], path


def test_readyz_not_ready_without_lists(tmp_path, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    from swift_screening.lists.store import ListStore
    from swift_screening.storage.db import Database

    db = Database(f"sqlite:///{tmp_path / 'x.db'}")
    db.create_all()
    svc = ScreeningService(ListStore(tmp_path / "none"), config, db)
    with TestClient(create_app(svc, config)) as c:
        assert c.get("/readyz").status_code == 503
