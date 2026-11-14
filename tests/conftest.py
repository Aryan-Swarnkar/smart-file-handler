"""Shared pytest fixtures.

CRITICAL ORDER: environment defaults are set BEFORE `app.main` is imported,
because `create_app()` runs at module import (module-level `app`) and reads
Settings at call time.
"""

from __future__ import annotations

import os

os.environ.setdefault("ENV", "test")
os.environ.setdefault(
    "SECRET_KEY", "unit-test-only-secret-key-0123456789abcdef0123456789abcdef"
)
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/smart_file_handler",
)
# Auth tests need a REAL redis (rate limiting) — compose redis on 6379.
# A unique namespace per run keeps rate-limit counters from leaking between
# pytest runs (Redis data outlives the test process).
# db 15 (not the dev stack's db 0): test keys stay isolated from dev traffic.
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault(
    "RATELIMIT_NAMESPACE", f"rl-test-{os.urandom(4).hex()}"
)
os.environ.setdefault("LOG_FORMAT", "json")

from collections.abc import AsyncGenerator, Generator

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from app.core.config import get_settings
from app.main import create_app


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Generator[None, None, None]:
    """Tests that monkeypatch env vars need a fresh Settings instance."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _flush_rate_limits() -> AsyncGenerator[None, None]:
    """Clear this run's auth rate-limit keys before each test.

    All API tests share client IP 127.0.0.1 — without this, an early test
    hammering login/registration would 429 the rest of the suite. Kept here
    (not in test modules) so every API module inherits it; P3's files suite
    does ~18 registrations per run and needs a clean counter per test.
    """
    namespace = get_settings().RATELIMIT_NAMESPACE
    redis = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    try:
        async for key in redis.scan_iter(f"{namespace}:auth:*"):
            await redis.delete(key)
        yield
    finally:
        await redis.aclose()


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    app = create_app()
    # httpx ASGITransport does NOT run lifespan; do it explicitly so
    # startup/shutdown (e.g. app.state.redis) is exercised like in prod.
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            yield ac
