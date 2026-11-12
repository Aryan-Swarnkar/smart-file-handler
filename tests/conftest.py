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
# P2: auth tests need a REAL redis (rate limiting) — compose redis on 6379.
# A unique namespace per run keeps rate-limit counters from leaking between
# pytest runs (Redis data outlives the test process).
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault(
    "RATELIMIT_NAMESPACE", f"rl-test-{os.urandom(4).hex()}"
)
os.environ.setdefault("LOG_FORMAT", "json")

from collections.abc import AsyncGenerator, Generator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import create_app


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Generator[None, None, None]:
    """Tests that monkeypatch env vars need a fresh Settings instance."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    app = create_app()
    # httpx ASGITransport does NOT run lifespan; do it explicitly so
    # startup/shutdown (e.g. app.state.redis) is exercised like in prod.
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            yield ac
