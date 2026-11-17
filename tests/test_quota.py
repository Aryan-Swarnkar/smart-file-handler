"""Quota accounting (Phase 5): atomic reserve/release, never read-modify-write.

The repository is driven directly (own engines + asyncio.gather, the
test_move_concurrency precedent) for the semantic corner cases and the
reserve race; the API is driven for the end-to-end 409/release paths.
Users are seeded with tiny quotas so boundary behavior costs bytes, not
gigabytes. Unique emails keep users hermetic across runs (rows persist).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db.models import File, User
from app.repositories.quota import QuotaRepository
from app.services.storage_service import StorageService
from tests.test_files import TXT_BYTES, _init, _upload

QUOTA = 1000


# --- fixtures / helpers ----------------------------------------------------------


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(get_settings().DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


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


async def _seed(
    db: AsyncSession, *, quota: int = QUOTA, used: int = 0
) -> User:
    """One account row directly in the DB (no argon2 — tokens are minted by
    the API's own registration when a test needs HTTP auth)."""
    user = User(
        email=f"quota-{uuid.uuid4().hex[:10]}@example.com",
        password_hash="x",
        storage_quota_bytes=quota,
        storage_used_bytes=used,
    )
    db.add(user)
    await db.commit()
    return user


async def _register(
    db: AsyncSession, client: AsyncClient, *, quota: int = QUOTA
) -> tuple[User, dict[str, str]]:
    """Register + login through the real API, then shrink the account's quota
    to `quota` so the tests exercise the boundary cheaply."""
    email = f"quota-{uuid.uuid4().hex[:10]}@example.com"
    resp = await client.post(
        "/auth/register", json={"email": email, "password": "correct-horse-1"}
    )
    assert resp.status_code == 201, resp.text
    resp = await client.post(
        "/auth/login", json={"email": email, "password": "correct-horse-1"}
    )
    assert resp.status_code == 200, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    row = (await db.execute(select(User).where(User.email == email))).scalar_one()
    row.storage_quota_bytes = quota
    row.storage_used_bytes = 0
    await db.commit()
    return row, headers


# --- registration defaults ---------------------------------------------------------


async def test_registration_grants_default_quota(
    client: AsyncClient, db: AsyncSession
) -> None:
    email = f"quota-{uuid.uuid4().hex[:10]}@example.com"
    resp = await client.post(
        "/auth/register", json={"email": email, "password": "correct-horse-1"}
    )
    assert resp.status_code == 201, resp.text
    row = (await db.execute(select(User).where(User.email == email))).scalar_one()
    assert row.storage_quota_bytes == get_settings().default_storage_quota_bytes
    assert row.storage_used_bytes == 0


# --- repository semantics -----------------------------------------------------------


async def test_repository_reserve_release_semantics(db: AsyncSession) -> None:
    user = await _seed(db)
    repo = QuotaRepository(db)

    assert await repo.reserve(user.id, 800) is True
    assert await repo.used_bytes(user.id) == 800

    assert await repo.reserve(user.id, 201) is False  # 1001 > 1000: one over
    assert await repo.used_bytes(user.id) == 800  # a miss leaves the counter

    assert await repo.reserve(user.id, 200) is True  # lands exactly at the cap
    assert await repo.used_bytes(user.id) == 1000

    assert await repo.release(user.id, 100) is True
    assert await repo.used_bytes(user.id) == 900

    assert await repo.release(user.id, 1000) is False  # cannot release > held
    assert await repo.used_bytes(user.id) == 900  # guard kept the counter

    assert await repo.release(user.id, 900) is True
    assert await repo.used_bytes(user.id) == 0

    assert await repo.release(user.id, 0) is True  # 0-byte release is a no-op
    assert await repo.used_bytes(user.id) == 0


async def test_concurrent_reserve_single_winner(
    db: AsyncSession, engine_a: AsyncEngine, engine_b: AsyncEngine
) -> None:
    """Two concurrent reservations of the same 600 bytes against a 1000-byte
    quota: the conditional UPDATE decides inside PostgreSQL — exactly one
    wins, the loser is a plain False, and the counter moves exactly once."""
    user = await _seed(db)

    async def attempt(engine: AsyncEngine) -> bool:
        session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)()
        try:
            allowed = await QuotaRepository(session).reserve(user.id, 600)
            # The repository is a transaction unit, not a commit point — the
            # production caller (FileService.init_upload) commits; mirroring
            # that here is what makes the row lock visible to the racer.
            await session.commit()
            return allowed
        finally:
            await session.close()

    results = await asyncio.gather(attempt(engine_a), attempt(engine_b))
    assert sorted(results) == [False, True], results
    assert await QuotaRepository(db).used_bytes(user.id) == 600


# --- API end-to-end ------------------------------------------------------------------


async def test_init_reserves_owner_quota(
    client: AsyncClient, db: AsyncSession
) -> None:
    user, headers = await _register(db, client)
    init = await _init(client, headers, "keep.jpg", 400)
    assert init.status_code == 201, init.text
    assert await QuotaRepository(db).used_bytes(user.id) == 400


async def test_quota_exceeded_409_no_row_leak(
    client: AsyncClient, db: AsyncSession
) -> None:
    user, headers = await _register(db, client)
    first = await _init(client, headers, "big.bin", 800)
    assert first.status_code == 201, first.text

    blocked = await _init(client, headers, "over.bin", 201)  # 1001 > 1000
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "quota_exceeded"
    rows = (
        (await db.execute(select(File).where(File.user_id == user.id))).scalars().all()
    )
    assert len(rows) == 1  # the rejected init left no row behind
    assert await QuotaRepository(db).used_bytes(user.id) == 800

    exact = await _init(client, headers, "exact.bin", 200)  # fills to the cap
    assert exact.status_code == 201, exact.text
    assert await QuotaRepository(db).used_bytes(user.id) == 1000

    last = await _init(client, headers, "one.bin", 1)  # nothing left
    assert last.status_code == 409
    assert last.json()["error"]["code"] == "quota_exceeded"


async def test_upload_not_found_keeps_reservation(
    client: AsyncClient, db: AsyncSession
) -> None:
    """complete without any PUT → 409, reservation KEPT (the presigned URL
    may still be live for a re-PUT; the Phase 6 sweeper reaps abandonment)."""
    user, headers = await _register(db, client)
    init = await _init(client, headers, "ghost.bin", 100)
    assert init.status_code == 201, init.text
    resp = await client.post(f"/files/{init.json()['file_id']}/complete", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "upload_not_found"
    assert await QuotaRepository(db).used_bytes(user.id) == 100


async def test_size_mismatch_releases_and_zeroes(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Two layers of size defense. (1) The presigned PUT is length-bound
    (ContentLength rides in the SigV4 signed headers): a lying client's PUT
    is rejected with 403 — no object exists, so complete is
    upload_not_found and the reservation is KEPT for a re-PUT. (2) If an
    object ever appears under the key with a foreign size (planted here via
    a direct put_bytes), complete kills object + reservation and zeroes the
    row so a later delete cannot double-release."""
    user, headers = await _register(db, client)
    init = await _init(client, headers, "liar.bin", 100)
    assert init.status_code == 201, init.text
    file_id = uuid.UUID(init.json()["file_id"])

    async with AsyncClient() as real:
        put = await real.put(init.json()["upload_url"], content=TXT_BYTES)
    assert put.status_code == 403, f"PUT should be length-bound: {put.status_code}"

    resp = await client.post(f"/files/{file_id}/complete", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "upload_not_found"
    assert await QuotaRepository(db).used_bytes(user.id) == 100  # KEPT

    row = await db.get(File, file_id)
    assert row is not None
    storage = StorageService()
    await storage.start()
    try:
        await storage.put_bytes(row.storage_key, TXT_BYTES)  # wrong size on purpose
    finally:
        await storage.close()

    resp = await client.post(f"/files/{file_id}/complete", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "size_mismatch"
    assert resp.json()["error"]["details"] == {
        "declared": 100,
        "actual": len(TXT_BYTES),
    }
    assert await QuotaRepository(db).used_bytes(user.id) == 0  # released

    row = await db.get(File, file_id)
    await db.refresh(row)  # the test session cached this row BEFORE the API commit
    assert row is not None and row.size_bytes == 0  # zeroed: no double-release
    assert row.status.value == "pending_upload"  # transition never happened

    detail = await client.get(f"/files/{file_id}", headers=headers)
    assert detail.status_code == 200
    async with AsyncClient() as real:
        fetch = await real.get(detail.json()["url"])
    assert fetch.status_code != 200  # the killed object is gone from S3

    # Deleting the husk must not double-release (release guard holds at 0).
    assert (
        await client.delete(f"/files/{file_id}", headers=headers)
    ).status_code == 204
    assert await QuotaRepository(db).used_bytes(user.id) == 0


async def test_delete_releases_reservation(
    client: AsyncClient, db: AsyncSession
) -> None:
    user, headers = await _register(db, client)
    resp = await _upload(client, headers, "doomed.txt", TXT_BYTES)
    assert resp.status_code == 202, resp.text
    assert await QuotaRepository(db).used_bytes(user.id) == len(TXT_BYTES)
    assert (
        await client.delete(f"/files/{resp.json()['id']}", headers=headers)
    ).status_code == 204
    assert await QuotaRepository(db).used_bytes(user.id) == 0
