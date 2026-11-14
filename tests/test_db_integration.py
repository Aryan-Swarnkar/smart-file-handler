"""Integration tests against the migrated compose DB (localhost:5433).

These run inside a transaction that is ALWAYS rolled back — zero residue.
Requires: docker compose up postgres + alembic upgrade head applied.

Phase 1 note: the pgvector/faces roundtrip test died with the CUT faces
feature; what remains covers the v3-relevant tables.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import File, FileKind, FileStatus, User


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """DB session for integration tests.

    NullPool + explicit dispose(): the module-global engine pools connections
    bound to one event loop; pytest-asyncio creates a fresh loop per test, and
    a reused socket from a dead loop crashes (proactor NoneType.send). So: no
    pooling across tests, no pooled leftovers — prod code path is untouched.
    """
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, autoflush=False, expire_on_commit=False
    )
    try:
        async with factory() as session:
            try:
                yield session
            finally:
                # No commit ever happens in these tests: everything rolls back.
                await session.rollback()
    finally:
        await engine.dispose()


async def test_enum_values_persist(db: AsyncSession) -> None:
    """Native PG enums store the .value strings, not Python names."""
    user = User(email="enum-test@example.com", password_hash="x")
    db.add(user)
    await db.flush()
    f = File(
        user_id=user.id,
        original_name="t.txt",
        mime_type="text/plain",
        size_bytes=3,
        sha256="1" * 64,
        storage_key=f"{user.id}/t.txt",
        status=FileStatus.DONE,
        kind=FileKind.TEXT,
        doc_topic="other",
    )
    db.add(f)
    await db.flush()
    row = await db.execute(
        select(File.status, File.kind, File.doc_topic).where(File.id == f.id)
    )
    status, kind, topic = row.one()
    assert status == FileStatus.DONE
    assert kind == FileKind.TEXT
    assert topic == "other"
