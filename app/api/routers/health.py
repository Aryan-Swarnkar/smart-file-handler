"""Liveness, readiness, and service index.

Liveness (`/healthz`) must never touch dependencies: if the process is up, it
answers — otherwise the orchestrator would restart-loop a DB outage.
Readiness (`/readyz`) checks each dependency and names the failing one.
Scope note: redis only at P0; DB joins in P1, storage in P3.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe (dependencies)")
async def readyz(request: Request) -> JSONResponse:
    checks: dict[str, str] = {}
    ok = True

    redis: Redis | None = getattr(request.app.state, "redis", None)
    if redis is None:
        checks["redis"] = "not_configured"
        ok = False
    else:
        try:
            await redis.ping()
            checks["redis"] = "up"
        except (RedisError, OSError):
            checks["redis"] = "down"
            ok = False

    # DB/storage checks append here in P1/P3.
    body: dict[str, Any] = {"status": "ready" if ok else "unavailable", "checks": checks}
    return JSONResponse(
        status_code=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=body,
    )


@router.get("/", summary="Service index", include_in_schema=False)
async def index() -> dict[str, str]:
    return {
        "service": "smart-file-handler",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/healthz",
        "ready": "/readyz",
        "metrics": "/metrics",
    }
