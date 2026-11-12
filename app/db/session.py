"""Async engine + session factory.

- One session per request via the get_session dependency.
- expire_on_commit=False: after commit, ORM objects stay usable without a
  second SELECT (avoids implicit IO surprises inside async handlers).
- pool_pre_ping: survive DB restarts transparently.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

engine: AsyncEngine = create_async_engine(
    get_settings().DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=1800,
)

session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: exactly one session per request, always closed."""
    async with session_factory() as session:
        yield session
