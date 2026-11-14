"""P4 worker tests: pipeline idempotency, retry→failed, sweep recovery, and
the API's enqueue path — all against the REAL compose DB + Redis (db 15, see
conftest: the running dev worker listens on db 0 and must never consume test
jobs). The job coroutines are invoked directly with a hand-built `ctx`; a
separate live pass exercises the actual worker process end-to-end.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.worker import Retry
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db.models import File, FileKind, FileStatus, User
from app.workers import pipeline
from app.workers.pipeline import PROCESS_JOB_PREFIX, process_file, sweep_uploads


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """NullPool session factory per test (the P1 lesson: the module-global
    engine's pooled connections belong to the loop of whichever test created
    them; pytest-asyncio makes a fresh loop per test → proactor NoneType.send).
    Passed to the jobs via ctx — the same path the real worker uses."""
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, autoflush=False, expire_on_commit=False
    )
    yield factory
    await engine.dispose()


async def _make_user(
    factory: async_sessionmaker[AsyncSession], label: str
) -> uuid.UUID:
    uid = uuid.uuid4()
    async with factory() as s:
        s.add(
            User(
                id=uid,
                email=f"worker-{label}-{uid.hex[:8]}@example.com",
                password_hash="unused-here",
            )
        )
        await s.commit()
    return uid


async def _make_file(
    factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    *,
    sha256: str | None,
    status: FileStatus = FileStatus.UPLOADED,
    kind: FileKind | None = FileKind.TEXT,
) -> File:
    file = File(
        user_id=user_id,
        original_name="worker-test.txt",
        mime_type="text/plain",
        size_bytes=8,
        sha256=sha256,
        storage_key=f"{user_id}/{uuid.uuid4()}.txt",
        status=status,
        kind=kind,
    )
    async with factory() as s:
        s.add(file)
        await s.commit()
    return file


async def _get_file(
    factory: async_sessionmaker[AsyncSession], fid: uuid.UUID
) -> File | None:
    async with factory() as s:
        return await s.get(File, fid)


@pytest.fixture
async def arq_pool() -> AsyncGenerator[ArqRedis, None]:
    pool = await create_pool(RedisSettings.from_dsn(get_settings().REDIS_URL))
    yield pool
    await pool.aclose()


# --- process_file ---------------------------------------------------------------


async def test_process_file_claims_and_completes(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _make_user(db_factory, "happy")
    file = await _make_file(db_factory, uid, sha256="a" * 64)
    result = await process_file({"session_factory": db_factory}, str(file.id))
    assert result == "done"
    row = await _get_file(db_factory, file.id)
    assert row is not None and row.status is FileStatus.DONE


async def test_process_file_skips_when_already_done(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    uid = await _make_user(db_factory, "done")
    file = await _make_file(db_factory, uid, sha256="b" * 64, status=FileStatus.DONE)
    assert (
        await process_file({"session_factory": db_factory}, str(file.id))
        == "skipped"
    )
    row = await _get_file(db_factory, file.id)
    assert row is not None and row.status is FileStatus.DONE


async def test_process_file_missing_id(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert (
        await process_file({"session_factory": db_factory}, str(uuid.uuid4()))
        == "missing"
    )


async def test_process_file_refuses_init_row(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """sha256 NULL → bytes were never PUT → never claim (defensive: the sweep
    excludes these too)."""
    uid = await _make_user(db_factory, "init")
    file = await _make_file(db_factory, uid, sha256=None)
    assert (
        await process_file({"session_factory": db_factory}, str(file.id))
        == "incomplete"
    )
    row = await _get_file(db_factory, file.id)
    assert row is not None and row.status is FileStatus.UPLOADED


async def test_process_file_retry_raises_retry(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = await _make_user(db_factory, "retry")
    file = await _make_file(db_factory, uid, sha256="c" * 64)

    async def boom(file: File) -> None:
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(pipeline, "_run_pipeline", boom)
    ctx = {"session_factory": db_factory, "job_try": 1}
    with pytest.raises(Retry) as exc_info:
        await process_file(ctx, str(file.id))
    # 5s base * 2^0 → defer_score is milliseconds (arq stores int ms)
    assert exc_info.value.defer_score == 5000
    row = await _get_file(db_factory, file.id)
    assert row is not None and row.status is FileStatus.PROCESSING


async def test_process_file_terminal_failure_marks_failed(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = await _make_user(db_factory, "failed")
    file = await _make_file(db_factory, uid, sha256="d" * 64)

    async def boom(file: File) -> None:
        raise RuntimeError("no more tries")

    monkeypatch.setattr(pipeline, "_run_pipeline", boom)
    ctx = {"session_factory": db_factory, "job_try": 3}
    result = await process_file(ctx, str(file.id))
    assert result == "failed"
    row = await _get_file(db_factory, file.id)
    assert row is not None
    assert row.status is FileStatus.FAILED
    assert row.error == "no more tries"


# --- sweep_uploads -----------------------------------------------------------------


async def test_sweep_enqueues_unclaimed_completed(
    db_factory: async_sessionmaker[AsyncSession],
    arq_pool: ArqRedis,
) -> None:
    uid = await _make_user(db_factory, "sweep")
    file = await _make_file(db_factory, uid, sha256="e" * 64)
    job_key = f"arq:job:{PROCESS_JOB_PREFIX}{file.id}"
    ctx = {"redis": arq_pool, "session_factory": db_factory}

    # NOTE: assertions are row-specific, never global counts — the dev DB
    # legitimately contains unclaimed rows from earlier runs/crashes, and the
    # sweep is SUPPOSED to pick all of them up (P3's pollution lesson, P4
    # edition: the sweep's contract is global, so tests must scope themselves).
    import redis.asyncio as aioredis

    redis = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
    try:
        await redis.delete(job_key)  # fresh state for OUR row
        await sweep_uploads(ctx)
        assert await redis.exists(job_key)

        # Idempotency: the queue zset score IS the enqueue timestamp — a
        # second sweep must not re-enqueue (a re-enqueue would bump it).
        score1 = await redis.zscore("arq:queue", f"{PROCESS_JOB_PREFIX}{file.id}")
        await sweep_uploads(ctx)
        score2 = await redis.zscore("arq:queue", f"{PROCESS_JOB_PREFIX}{file.id}")
        assert score1 is not None and score2 == score1
    finally:
        await redis.aclose()

    # The sweep enqueued the REAL job the worker runs:
    result = await process_file({"session_factory": db_factory}, str(file.id))
    assert result == "done"


async def test_sweep_ignores_plain_init_rows(
    db_factory: async_sessionmaker[AsyncSession],
    arq_pool: ArqRedis,
) -> None:
    uid = await _make_user(db_factory, "sweep-init")
    file = await _make_file(db_factory, uid, sha256=None)  # init row: nothing
    await sweep_uploads({"redis": arq_pool, "session_factory": db_factory})
    job_key = f"arq:job:{PROCESS_JOB_PREFIX}{file.id}"
    import redis.asyncio as aioredis

    redis = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
    try:
        assert not await redis.exists(job_key)
    finally:
        await redis.aclose()
    row = await _get_file(db_factory, file.id)
    assert row is not None  # untouched


async def test_sweep_deletes_stale_orphan_inits(
    db_factory: async_sessionmaker[AsyncSession],
    arq_pool: ArqRedis,
) -> None:
    uid = await _make_user(db_factory, "sweep-orphan")
    stale = await _make_file(db_factory, uid, sha256=None)
    fresh = await _make_file(db_factory, uid, sha256=None)
    # Backdate the stale one past the grace window (created_at is server now()).
    old = datetime.now(UTC) - timedelta(
        minutes=get_settings().ORPHAN_INIT_MINUTES + 30
    )
    async with db_factory() as s:
        await s.execute(update(File).where(File.id == stale.id).values(created_at=old))
        await s.commit()

    await sweep_uploads({"redis": arq_pool, "session_factory": db_factory})

    async with db_factory() as s:
        rows = (
            (await s.execute(select(File).where(File.user_id == uid)))
            .scalars()
            .all()
        )
    ids = {r.id for r in rows}
    assert stale.id not in ids
    assert fresh.id in ids  # recent init survives the grace window


# --- API enqueue path --------------------------------------------------------------


async def test_api_complete_enqueues_job(client: AsyncClient) -> None:
    """Full API round trip: register → init → PUT → complete; the processable
    file must land in the ARQ queue with the canonical job id."""
    email = f"enq-{uuid.uuid4().hex[:8]}@example.com"
    password = "correct-horse-1"
    r = await client.post(
        "/auth/register", json={"email": email, "password": password}
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/auth/login", json={"email": email, "password": password}
    )
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        "/files/init",
        json={
            "filename": "queue-me.txt",
            "content_type": "text/plain",
            "size": 5,
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    file_id = body["file_id"]

    # Real bytes to the real SeaweedFS (the ASGI transport cannot proxy S3).
    import httpx

    async with httpx.AsyncClient() as s3:
        put = await s3.put(body["upload_url"], content=b"hello")
    assert put.status_code == 200, put.text

    r = await client.post(f"/files/{file_id}/complete", headers=headers)
    assert r.status_code == 200, r.text

    import redis.asyncio as aioredis

    from app.core.config import get_settings as gs

    redis = aioredis.from_url(gs().REDIS_URL, decode_responses=True)
    try:
        assert await redis.exists(f"arq:job:{PROCESS_JOB_PREFIX}{file_id}")
    finally:
        await redis.aclose()

