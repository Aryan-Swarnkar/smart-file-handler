"""App factory.

`create_app()` exists for tests/future multi-config needs; the module-level
`app` is what uvicorn and docker-compose run (`uvicorn app.main:app`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis

from app.api.routers import auth, files, health
from app.core.config import Settings, get_settings
from app.core.errors import install_error_handlers
from app.core.logging import RequestIdMiddleware, configure_logging, get_logger
from app.db.session import engine
from app.services.storage import StorageService

logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Stateless API: the only "state" we keep is a connection pool, which is
        # required for N replicas behind an LB and is not user-visible state.
        app.state.redis = Redis.from_url(
            settings.REDIS_URL, decode_responses=True, socket_connect_timeout=2
        )
        # Object storage client (S3-compatible: SeaweedFS dev, S3/R2 prod).
        # The bucket is created idempotently here so replicas never depend on
        # an init container.
        storage = StorageService()
        await storage.start()
        app.state.storage = storage
        logger.info("startup_complete", env=settings.ENV)
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await storage.close()
            await engine.dispose()
            logger.info("shutdown_complete")

    app = FastAPI(
        title="smart-file-handler",
        version="0.1.0",
        description="Auto-organizes uploaded files into smart categories.",
        lifespan=lifespan,
    )

    app.add_middleware(RequestIdMiddleware)

    origins = settings.cors_origins
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["x-request-id"],
        )

    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(files.router)

    # Explicit route instead of mounting make_asgi_app(): a mount 307-redirects
    # /metrics -> /metrics/, which looks broken to curl and simple scrapers.
    @app.get("/metrics", include_in_schema=False, tags=["health"])
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
