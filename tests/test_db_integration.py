"""Integration tests against the migrated compose DB (localhost:5433).

These run inside a transaction that is ALWAYS rolled back — zero residue.
Requires: docker compose up postgres + alembic upgrade head applied.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import FaceInstance, File, FileKind, FileStatus, Label, LabelKind, Person, User

DIM = 512


def _vec(base: float) -> list[float]:
    return [base] * DIM


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


async def test_pgvector_roundtrip_and_nn(db: AsyncSession) -> None:
    user = User(email="vector-test@example.com", password_hash="x")
    db.add(user)
    await db.flush()

    f = File(
        user_id=user.id,
        original_name="a.jpg",
        mime_type="image/jpeg",
        size_bytes=1,
        sha256="0" * 64,
        storage_key=f"{user.id}/a.jpg",
        status=FileStatus.DONE,
        kind=FileKind.IMAGE,
    )
    db.add(f)
    await db.flush()

    p = Person(user_id=user.id, centroid=_vec(0.5), face_count=2)
    f2 = FaceInstance(
        file_id=f.id,
        user_id=user.id,
        person_id=p.id,
        embedding=_vec(0.5),
        bbox=[0.0, 0.0, 1.0, 1.0],
        det_score=0.9,
    )
    f2_other = FaceInstance(
        file_id=f.id,
        user_id=user.id,
        embedding=_vec(-0.5),  # deliberately different cluster
        bbox=[2.0, 2.0, 3.0, 3.0],
        det_score=0.8,
    )
    db.add_all([p, f2, f2_other])
    await db.flush()

    label = Label(user_id=user.id, name="dog", kind=LabelKind.THING)
    db.add(label)
    await db.flush()

    # --- cosine NN: exact nearest to the 0.5 vector must be f2's embedding ---
    result = await db.execute(
        select(FaceInstance.id)
        .order_by(FaceInstance.embedding.cosine_distance(_vec(0.5)))
        .limit(1)
    )
    nearest = result.scalar_one()
    assert nearest == f2.id

    # --- distance to an identical vector is ~0 (cosine_distance in pgvector) ---
    dist = await db.execute(
        select(FaceInstance.embedding.cosine_distance(_vec(0.5))).where(
            FaceInstance.id == f2.id
        )
    )
    assert dist.scalar_one() == pytest.approx(0.0, abs=1e-6)

    # --- label roundtrip ---
    name = await db.execute(select(Label.name).where(Label.kind == LabelKind.THING))
    assert name.scalar_one() == "dog"


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
