"""Concurrent-move safety: the per-owner pg_advisory_xact_lock + check-after-
lock ordering must make cycle creation impossible, no matter how two racing
movers interleave.

These tests drive FolderService.move on TWO SEPARATE sessions (own engines,
NullPool) via asyncio.gather — exactly the production race: one session per
request, same owner, same DB.

Invariants asserted after every race:
- exactly one opposing move wins; the loser raises 422 folder_cycle and its
  UPDATE never lands
- the final tree contains no cycle (parent walk terminates at the root)
- independent moves both succeed (the lock serializes, never rejects)
- same-direction double moves both succeed (idempotent re-parent)
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.errors import DomainValidationError
from app.repositories.folders import FolderRepository
from app.services.folder_service import FolderService
from tests.authz_matrix.conftest import commit, seed_user


@pytest.fixture
async def engine_a() -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(get_settings().DATABASE_URL, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def engine_b() -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(get_settings().DATABASE_URL, poolclass=NullPool)
    yield engine
    await engine.dispose()


def _factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _attempt_move(
    factory: async_sessionmaker[AsyncSession],
    owner_id: uuid.UUID,
    folder_id: uuid.UUID,
    dest_id: uuid.UUID,
) -> tuple[str, str | None]:
    """One move as one transaction. Returns ("ok", new_parent) or
    ("conflict", error_code). The session closes after, releasing the
    advisory lock either way."""
    async with factory() as session:
        try:
            folder = await FolderService(session).move(owner_id, folder_id, dest_id)
        except DomainValidationError as exc:
            await session.rollback()
            return "conflict", exc.code
        return "ok", str(folder.parent_id)


async def _walk_to_root(
    session: AsyncSession, folder_id: uuid.UUID
) -> list[uuid.UUID]:
    """Follow parent_id pointers (fresh reads); a legal tree reaches NULL
    within the depth cap and never revisits a node."""
    from sqlalchemy import select

    from app.db.models import Folder

    seen: list[uuid.UUID] = []
    current: uuid.UUID | None = folder_id
    while current is not None:
        assert current not in seen, f"cycle detected: {[*seen, current]}"
        seen.append(current)
        assert len(seen) <= 21, "walk exceeded the depth cap"
        parent = (
            await session.execute(
                select(Folder.parent_id).where(Folder.id == current)
            )
        ).scalar_one_or_none()
        current = parent
    return seen


async def _seed_two_siblings(
    db: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """owner + root + A + B, all siblings under the root."""
    owner = await seed_user(db)
    from tests.authz_matrix.conftest import seed_folder

    root = await seed_folder(db, owner.id, name="root")
    a = await seed_folder(db, owner.id, parent_id=root.id, name="A")
    b = await seed_folder(db, owner.id, parent_id=root.id, name="B")
    await commit(db)
    return owner.id, root.id, a.id, b.id


@pytest.mark.parametrize("iteration", range(5))
async def test_opposing_moves_cannot_cycle(
    db: AsyncSession, engine_a: AsyncEngine, engine_b: AsyncEngine, iteration: int
) -> None:
    owner_id, root_id, a_id, b_id = await _seed_two_siblings(db)
    factory_a, factory_b = _factory(engine_a), _factory(engine_b)

    results = await asyncio.gather(
        _attempt_move(factory_a, owner_id, a_id, b_id),  # A under B
        _attempt_move(factory_b, owner_id, b_id, a_id),  # B under A
    )
    outcomes = [r[0] for r in results]
    codes = [r[1] for r in results]

    assert sorted(outcomes) == ["conflict", "ok"], results
    loser_code = codes[outcomes.index("conflict")]
    assert loser_code == "folder_cycle", codes

    # Exactly one edge landed, and the tree is walkable to the root.
    winner_parent = uuid.UUID(results[outcomes.index("ok")][1])
    check = _factory(engine_a)()
    try:
        walk = await _walk_to_root(check, winner_parent)
        assert walk[-1] == root_id, f"winner is not rooted at root: {walk}"
        loser_id = a_id if winner_parent == b_id else b_id
        loser_walk = await _walk_to_root(check, loser_id)
        assert loser_walk[-1] == root_id, f"loser walk: {loser_walk}"
    finally:
        await check.close()


@pytest.mark.parametrize("iteration", range(3))
async def test_same_direction_moves_both_succeed(
    db: AsyncSession, engine_a: AsyncEngine, engine_b: AsyncEngine, iteration: int
) -> None:
    owner_id, _root, a_id, b_id = await _seed_two_siblings(db)
    factory_a, factory_b = _factory(engine_a), _factory(engine_b)

    results = await asyncio.gather(
        _attempt_move(factory_a, owner_id, a_id, b_id),
        _attempt_move(factory_b, owner_id, a_id, b_id),  # same move, twice
    )
    assert [r[0] for r in results] == ["ok", "ok"], results
    assert all(r[1] == str(b_id) for r in results), results


@pytest.mark.parametrize("iteration", range(3))
async def test_independent_moves_both_succeed(
    db: AsyncSession, engine_a: AsyncEngine, engine_b: AsyncEngine, iteration: int
) -> None:
    """Two unrelated subtrees of the SAME owner: the advisory lock
    serializes them (briefly) but must never reject either."""
    owner = await seed_user(db)
    from tests.authz_matrix.conftest import seed_folder

    root = await seed_folder(db, owner.id, name="root")
    a = await seed_folder(db, owner.id, parent_id=root.id, name="A")
    b = await seed_folder(db, owner.id, parent_id=root.id, name="B")
    dest = await seed_folder(db, owner.id, parent_id=root.id, name="dest")
    await commit(db)

    results = await asyncio.gather(
        _attempt_move(_factory(engine_a), owner.id, a.id, dest.id),
        _attempt_move(_factory(engine_b), owner.id, b.id, dest.id),
    )
    assert [r[0] for r in results] == ["ok", "ok"], results


async def test_locked_check_rejects_a_move_raced_by_trash(
    db: AsyncSession, engine_a: AsyncEngine, engine_b: AsyncEngine
) -> None:
    """Move vs trash race: if trash commits before the mover's post-lock
    re-read, the mover must NOT drag a trashed folder around."""
    from datetime import UTC, datetime

    owner_id, _root, a_id, b_id = await _seed_two_siblings(db)

    # Trash A first (committed), THEN move A — the fresh re-read must 422.
    trash_session = _factory(engine_a)()
    try:
        folder = await FolderRepository(trash_session).get_fresh(a_id)
        assert folder is not None
        folder.deleted_at = datetime.now(UTC)
        await trash_session.commit()
    finally:
        await trash_session.close()

    result = await _attempt_move(_factory(engine_b), owner_id, a_id, b_id)
    assert result == ("conflict", "trashed"), result
