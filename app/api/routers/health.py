"""Liveness, readiness, and service index.

Liveness (`/healthz`) must never touch dependencies: if the process is up, it
answers — otherwise the orchestrator would restart-loop a DB outage.
Readiness (`/readyz`) checks each dependency and names the failing one.
Scope note: redis + postgres (P0/P1); storage (S3) joined in P3.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text

from app.db.session import session_factory

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

    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        checks["db"] = "up"
    except OSError:
        checks["db"] = "down"
        ok = False
    except Exception:
        checks["db"] = "down"
        ok = False

    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        checks["s3"] = "not_configured"
        ok = False
    else:
        try:
            await storage.ping()
            checks["s3"] = "up"
        except Exception:
            checks["s3"] = "down"
            ok = False

    # Storage check appends here in P3.
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
