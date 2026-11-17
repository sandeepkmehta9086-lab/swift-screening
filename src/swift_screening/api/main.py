"""ASGI entry point: ``uvicorn swift_screening.api.main:app``."""

from __future__ import annotations

import logging
from pathlib import Path

import structlog
from fastapi import FastAPI

from swift_screening.api.app import create_app
from swift_screening.config.models import load_config
from swift_screening.lists.store import ListStore
from swift_screening.service import ScreeningService
from swift_screening.settings import Settings
from swift_screening.storage.db import Database


def configure_logging(json_logs: bool) -> None:
    processors: list[structlog.types.Processor] = [
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=processors, wrapper_class=structlog.make_filtering_bound_logger(logging.INFO)
    )


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_json)
    if settings.database_url.startswith("sqlite:///"):
        Path(settings.database_url.removeprefix("sqlite:///")).parent.mkdir(
            parents=True, exist_ok=True
        )
    db = Database(settings.database_url)
    db.create_all()
    config = load_config(settings.config)
    service = ScreeningService(ListStore(settings.list_store), config, db)
    return create_app(service, config)


def __getattr__(name: str) -> FastAPI:  # lazy so importing the module has no side effects
    if name == "app":
        return build_app()
    raise AttributeError(name)
