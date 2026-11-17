"""Shared fixtures. All watchlists used here are fictional (fixtures/lists)."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from fastapi.testclient import TestClient

from swift_screening.api.app import create_app
from swift_screening.config.models import AppConfig, TenantConfig, hash_api_key, load_config
from swift_screening.lists.model import ListType
from swift_screening.lists.registry import connector_for, refresh_list
from swift_screening.lists.store import ListStore
from swift_screening.service import ScreeningService
from swift_screening.storage.db import Database

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "lists"
CONFIG = ROOT / "config" / "tenants.example.yaml"
TEST_KEY = "test-key-a"
OTHER_KEY = "test-key-b"
PUBLIC_KEY = "test-key-public"
STATELESS_KEY = "test-key-stateless"

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
# Keep test output readable: the service logs one line per screening at INFO.
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))


def load_fixture_lists(store: ListStore) -> None:
    refresh_list(
        store,
        connector_for("json", "FIXTURE_SANCTIONS", ListType.SANCTIONS),
        str(FIXTURES / "fixture_sanctions.json"),
    )
    refresh_list(
        store,
        connector_for("json", "FIXTURE_PEP", ListType.PEP),
        str(FIXTURES / "fixture_pep.json"),
    )
    refresh_list(
        store,
        connector_for("csv", "FIXTURE_INTERNAL", ListType.INTERNAL),
        str(FIXTURES / "fixture_internal.csv"),
    )


@pytest.fixture(scope="session")
def session_store(tmp_path_factory: pytest.TempPathFactory) -> ListStore:
    store = ListStore(tmp_path_factory.mktemp("lists"))
    load_fixture_lists(store)
    return store


@pytest.fixture
def store(tmp_path: Path) -> ListStore:
    s = ListStore(tmp_path / "lists")
    load_fixture_lists(s)
    return s


def make_config(**overrides: object) -> AppConfig:
    base = load_config(CONFIG)
    demo = base.tenant("demo")
    assert demo is not None
    tenants = [
        demo.model_copy(
            update={"tenant_id": "t1", "api_key_sha256": [hash_api_key(TEST_KEY)], **overrides}
        ),
        demo.model_copy(update={"tenant_id": "t2", "api_key_sha256": [hash_api_key(OTHER_KEY)]}),
        demo.model_copy(
            update={
                "tenant_id": "pub",
                "public_mode": True,
                "stateless": True,
                "rate_limit_per_minute": 5,
                "api_key_sha256": [hash_api_key(PUBLIC_KEY)],
            }
        ),
        demo.model_copy(
            update={
                "tenant_id": "sl",
                "stateless": True,
                "api_key_sha256": [hash_api_key(STATELESS_KEY)],
            }
        ),
    ]
    return AppConfig(tenants=tenants)


@pytest.fixture(scope="session")
def config() -> AppConfig:
    return make_config()


@pytest.fixture(scope="session")
def tenant(config: AppConfig) -> TenantConfig:
    t = config.tenant("t1")
    assert t is not None
    return t


@pytest.fixture(scope="session")
def service(session_store: ListStore, config: AppConfig) -> ScreeningService:
    """Service without persistence: fast, for matching scenarios."""
    return ScreeningService(session_store, config)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(f"sqlite:///{tmp_path / 'screening.db'}")
    d.create_all()
    return d


@pytest.fixture
def service_db(store: ListStore, config: AppConfig, db: Database) -> ScreeningService:
    return ScreeningService(store, config, db)


@pytest.fixture
def client(service_db: ScreeningService, config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(service_db, config)) as c:
        yield c


def auth(key: str = TEST_KEY) -> dict[str, str]:
    return {"X-API-Key": key}
