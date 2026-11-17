"""FastAPI application: /v1/screen/{mt,mx,entity}, /v1/results/{id}, /v1/lists/status, health.

Decision support only: deployers remain responsible for their compliance obligations.
"""

import asyncio
import hashlib
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from swift_screening import __version__
from swift_screening.config.models import AppConfig, TenantConfig
from swift_screening.decision.models import ScreeningResult
from swift_screening.decision.reason_codes import ReasonCode
from swift_screening.lists.store import ListStatus
from swift_screening.observability.metrics import REGISTRY
from swift_screening.service import EntityRequest, Format, ScreeningService
from swift_screening.storage.db import StorageUnavailable

DISCLAIMER = (
    "Decision-support service. Results do not replace the deploying institution's compliance "
    "judgment; deployers remain responsible for their sanctions obligations."
)
MT_TYPES = {"text/plain", "application/x-swift-mt", "application/octet-stream"}
MX_TYPES = {"application/xml", "text/xml"}


class RateLimiter:
    """Per-key token bucket (in-process; use a shared store when running several replicas)."""

    def __init__(self) -> None:
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, per_minute: int) -> tuple[bool, float]:
        rate = per_minute / 60.0
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(per_minute), now))
            tokens = min(float(per_minute), tokens + (now - last) * rate)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                return True, 0.0
            self._buckets[key] = (tokens, now)
            return False, (1 - tokens) / rate if rate > 0 else 60.0


@dataclass
class Caller:
    tenant: TenantConfig
    key_id: str


class EntityBody(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    country: str | None = Field(None, min_length=2, max_length=2)
    date_of_birth: str | None = None
    entity_type: str = "unknown"
    identifiers: list[dict[str, str]] = Field(default_factory=list, max_length=50)


def create_app(service: ScreeningService, config: AppConfig) -> FastAPI:
    app = FastAPI(
        title="SWIFT Screening Service",
        version=__version__,
        description=DISCLAIMER,
        openapi_url="/openapi.json",
    )
    limiter = RateLimiter()
    app.state.service = service
    app.state.limiter = limiter

    def caller(
        x_api_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Caller:
        key = x_api_key
        if key is None and authorization and authorization.lower().startswith("bearer "):
            key = authorization[7:].strip()
        if not key:
            raise HTTPException(401, "missing API key", headers={"WWW-Authenticate": "ApiKey"})
        tenant = config.tenant_for_key(key)
        if tenant is None:
            raise HTTPException(401, "invalid API key", headers={"WWW-Authenticate": "ApiKey"})
        key_id = hashlib.sha256(key.encode()).hexdigest()[:16]
        ok, retry = limiter.allow(f"{tenant.tenant_id}:{key_id}", tenant.rate_limit_per_minute)
        if not ok:
            raise HTTPException(
                429, "rate limit exceeded", headers={"Retry-After": str(max(1, int(retry + 0.999)))}
            )
        return Caller(tenant, key_id)

    CallerDep = Annotated[Caller, Depends(caller)]

    async def read_body(request: Request, tenant: TenantConfig, allowed: set[str]) -> bytes:
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype not in allowed:
            raise HTTPException(415, f"content type must be one of {sorted(allowed)}")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > tenant.max_body_bytes:
            raise HTTPException(413, "payload too large")
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > tenant.max_body_bytes:
                raise HTTPException(413, "payload too large")
        if not body.strip():
            raise HTTPException(422, "empty body")
        return body

    async def run_screen(
        fmt: Format, body: bytes, tenant: TenantConfig, fn: Callable[[], ScreeningResult]
    ) -> ScreeningResult:
        try:
            return await asyncio.wait_for(
                run_in_threadpool(fn), timeout=tenant.screening_timeout_ms / 1000
            )
        except TimeoutError:
            return await run_in_threadpool(
                service.fail_closed, fmt, body, tenant, ReasonCode.TIMEOUT
            )

    @app.exception_handler(StorageUnavailable)
    async def _storage_down(_: Request, exc: StorageUnavailable) -> JSONResponse:
        return JSONResponse(
            {"detail": "storage unavailable; message not screened, retry later"},
            status_code=503,
            headers={"Retry-After": "5"},
        )

    @app.post("/v1/screen/mt", response_model=ScreeningResult, tags=["screening"])
    async def screen_mt(request: Request, who: CallerDep) -> ScreeningResult:
        body = await read_body(request, who.tenant, MT_TYPES)
        text = body.decode("utf-8", "replace")
        return await run_screen("MT", body, who.tenant, lambda: service.screen_mt(text, who.tenant))

    @app.post("/v1/screen/mx", response_model=ScreeningResult, tags=["screening"])
    async def screen_mx(request: Request, who: CallerDep) -> ScreeningResult:
        body = await read_body(request, who.tenant, MX_TYPES)
        return await run_screen("MX", body, who.tenant, lambda: service.screen_mx(body, who.tenant))

    @app.post("/v1/screen/entity", response_model=ScreeningResult, tags=["screening"])
    async def screen_entity(request: Request, who: CallerDep) -> ScreeningResult:
        body = await read_body(request, who.tenant, {"application/json"})
        try:
            parsed = EntityBody.model_validate_json(body)
        except Exception as exc:
            raise HTTPException(422, f"invalid entity request: {exc}") from exc
        req = EntityRequest(
            name=parsed.name,
            country=parsed.country,
            date_of_birth=parsed.date_of_birth,
            entity_type=parsed.entity_type,
            identifiers=parsed.identifiers,
        )
        return await run_screen(
            "ENTITY", body, who.tenant, lambda: service.screen_entity(req, who.tenant)
        )

    @app.get("/v1/results/{screening_id}", response_model=ScreeningResult, tags=["screening"])
    async def get_result(screening_id: str, who: CallerDep) -> ScreeningResult:
        result = await run_in_threadpool(service.get_result, who.tenant, screening_id)
        if result is None:
            raise HTTPException(404, "not found")
        return result

    @app.get("/v1/lists/status", response_model=list[ListStatus], tags=["lists"])
    async def lists_status(who: CallerDep) -> list[ListStatus]:
        t = who.tenant
        return [service.store.status(lid, t.lists.sla_for(lid)) for lid in t.lists.enabled]

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    async def readyz(response: Response) -> dict[str, Any]:
        db_ok = service.db.ping() if service.db is not None else True
        loaded = {
            lid: service.store.state(lid).current_version is not None
            for t in config.tenants
            for lid in t.lists.enabled
        }
        ready = db_ok and any(loaded.values())
        response.status_code = 200 if ready else 503
        return {"ready": ready, "database": db_ok, "lists_loaded": loaded}

    @app.get("/metrics", tags=["ops"])
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest(REGISTRY).decode(), media_type=CONTENT_TYPE_LATEST)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    return app
