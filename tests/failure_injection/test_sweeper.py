"""Sweeper clauses under injected failures.

Reap races are decided deterministically by ordering the competing writer
BEFORE the sweep — the rowcount gates must hold in both directions. The
relay pause (root conftest) keeps the live dev relay/worker out of these
assertions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import File, FileKind, FileStatus, IdempotencyKey, Outbox, User
from app.repositories.files import FileRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.quota import QuotaRepository
from app.services.storage_service import StorageService
from app.workers import relay as relay_module
from app.workers import sweepers as sweepers_module
from app.workers import tasks as tasks_module
from tests.failure_injection.conftest import (
    FakeBroker,
    age_created_at,
    age_upload_expiry,
    make_factory,
    seed_file,
    seed_user_with_quota,
    tiny_png,
)


async def run_sweep() -> dict[str, int]:
    engine, _ = make_factory()
    try:
        return await sweepers_module._sweep()
    finally:
        await engine.dispose()


async def run_expire_clause() -> int:
    engine, factory = make_factory()
    try:
        return await sweepers_module._expire_pending_uploads(factory)
    finally:
        await engine.dispose()


async def object_exists(key: str) -> bool:
    storage = StorageService()
    await storage.start()
    try:
        return (await storage.head(key)) is not None
    finally:
        await storage.close()


async def status_of(db: AsyncSession, file_id: uuid.UUID) -> FileStatus | None:
    return await FileRepository(db).get_status(file_id)


async def used_bytes(db: AsyncSession, user_id: uuid.UUID) -> int | None:
    return await QuotaRepository(db).used_bytes(user_id)


async def unpublished_reprocess_count(db: AsyncSession, file_id: uuid.UUID) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(Outbox)
            .where(
                Outbox.event_type == "file.reprocess",
                Outbox.published_at.is_(None),
                Outbox.payload["file_id"].astext == str(file_id),
            )
        )
    ).scalar_one()


async def seed_reserved_pending(
    db: AsyncSession, *, with_object: bool
) -> tuple[User, uuid.UUID]:
    """User + reserved quota + pending file (optionally the real object)."""
    user = await seed_user_with_quota(db)
    assert await QuotaRepository(db).reserve(user.id, 100)
    file = await seed_file(
        db,
        user,
        status=FileStatus.PENDING_UPLOAD,
        size_bytes=100,
        kind=None,
        upload_expires_at=datetime.now(UTC) + timedelta(seconds=900),
        plant_object=with_object,
    )
    await db.commit()
    return user, file.id


async def drop_pending(db: AsyncSession, user: User, file_id: uuid.UUID) -> None:
    """Test residue cleanup: an aged pending row stays expired-forever and
    would be reaped by the NEXT run's sweep calls, breaking that run's
    count assertions. Release the reservation and drop the row."""
    await QuotaRepository(db).release(user.id, 100)
    await db.execute(delete(File).where(File.id == file_id))
    await db.commit()


# --- grace window + live rows --------------------------------------------------------


async def test_live_and_in_grace_rows_are_never_reaped(db: AsyncSession) -> None:
    live_user, live_id = await seed_reserved_pending(db, with_object=False)
    grace_user, grace_id = await seed_reserved_pending(db, with_object=False)
    try:
        await age_upload_expiry(db, grace_id, 30)  # inside the 60s grace window

        await run_expire_clause()
        assert await status_of(db, live_id) is FileStatus.PENDING_UPLOAD
        assert await status_of(db, grace_id) is FileStatus.PENDING_UPLOAD
    finally:
        await drop_pending(db, live_user, live_id)
        await drop_pending(db, grace_user, grace_id)


# --- happy-path reap -------------------------------------------------------------------


async def test_expired_pending_reaped_object_gone_quota_released(db: AsyncSession) -> None:
    user, file_id = await seed_reserved_pending(db, with_object=True)
    file = await db.get(File, file_id)
    assert file is not None

    await age_upload_expiry(db, file_id, 600)  # long past the grace window

    assert await object_exists(file.storage_key)
    assert await run_expire_clause() == 1

    assert await status_of(db, file_id) is None
    assert await used_bytes(db, user.id) == 0
    assert not await object_exists(file.storage_key)


# --- reap races ------------------------------------------------------------------------


async def test_complete_wins_the_reap_race(db: AsyncSession) -> None:
    """The real interleave, step by step: the sweeper SELECTS its candidate
    while the row is still pending, deletes the object (S3, outside any tx),
    and only then runs its conditional DELETE — but the completer committed
    first. The rowcount gate must lose the race: no release (no quota leak),
    row intact, CRITICAL signal; the deleted object is the accepted
    residual, now visible."""
    user, file_id = await seed_reserved_pending(db, with_object=True)
    file = await db.get(File, file_id)
    assert file is not None

    await age_upload_expiry(db, file_id, 600)

    # 1. Sweeper selects its candidate — the row is still pending.
    engine, factory = make_factory()
    try:
        async with factory() as session:
            candidates = await FileRepository(session).select_expired_pending(
                grace_seconds=60
            )
            await session.rollback()
    finally:
        await engine.dispose()
    assert [c.id for c in candidates] == [file_id]

    # 2. Sweeper deletes the object (S3 step, before any DB write).
    assert await object_exists(file.storage_key)
    storage = StorageService()
    await storage.start()
    try:
        await storage.delete(file.storage_key)
    finally:
        await storage.close()

    # 3. The completer wins: conditional transition + outbox event, committed.
    await db.execute(
        update(File)
        .where(File.id == file_id, File.status == FileStatus.PENDING_UPLOAD)
        .values(status=FileStatus.UPLOADED, kind=FileKind.IMAGE)
    )
    await OutboxRepository(db).insert_event(
        event_type="file.uploaded", payload={"file_id": str(file_id)}
    )
    await db.commit()

    # 4. Sweeper's conditional DELETE loses the race: rowcount 0.
    engine, factory = make_factory()
    try:
        async with factory() as session:
            deleted = await FileRepository(session).delete_expired_pending(
                file_id, grace_seconds=60
            )
            await session.commit()
    finally:
        await engine.dispose()
    assert deleted is False

    assert await status_of(db, file_id) is FileStatus.UPLOADED  # intact
    assert await used_bytes(db, user.id) == 100  # NOT released by the sweeper
    assert not await object_exists(file.storage_key)  # the residual


async def test_delete_wins_the_reap_race_no_double_release(db: AsyncSession) -> None:
    user, file_id = await seed_reserved_pending(db, with_object=False)

    await age_upload_expiry(db, file_id, 600)

    # "delete": the API releases quota and removes the row first.
    await db.execute(
        delete(File).where(
            File.id == file_id, File.status == FileStatus.PENDING_UPLOAD
        )
    )
    assert await QuotaRepository(db).release(user.id, 100)
    await db.commit()

    assert await run_expire_clause() == 0
    assert await status_of(db, file_id) is None
    used = await used_bytes(db, user.id)
    assert used == 0, "the rowcount gate must prevent a double release"


# --- stuck rescue ------------------------------------------------------------------------


async def test_stuck_rescue_writes_exactly_one_reprocess(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(db, user, status=FileStatus.UPLOADED)
    await db.commit()

    await age_created_at(db, file.id, 30)  # past STUCK_FILE_MINUTES

    await run_sweep()
    assert await unpublished_reprocess_count(db, file.id) == 1

    # Worker still "down": the next sweep must dedupe, not stack another row.
    await run_sweep()
    assert await unpublished_reprocess_count(db, file.id) == 1


async def test_rescue_flows_through_relay_to_done(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    # Phase 7: the finishing delivery runs the real pipeline — real bytes.
    file = await seed_file(
        db, user, status=FileStatus.UPLOADED, plant_object=True, data=tiny_png()
    )
    await db.commit()

    await age_created_at(db, file.id, 30)

    await run_sweep()  # rescue: writes file.reprocess outbox row
    broker = FakeBroker()
    engine, _ = make_factory()
    try:
        await relay_module._cycle(engine, broker)  # relay publishes it
    finally:
        await engine.dispose()
    assert ("process_file", str(file.id)) in broker.sent

    await tasks_module._drive_state_machine(file.id)  # the worker's run
    assert await status_of(db, file.id) is FileStatus.DONE


# --- idempotency-key expiry ----------------------------------------------------------------


async def test_expired_idempotency_keys_purged_live_kept(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    marker = uuid.uuid4().hex[:8]  # unique per run: prior runs' rows must not count
    expired = IdempotencyKey(
        user_id=user.id,
        key=f"expired-{marker}",
        request_hash="h",
        response_status=201,
        response_body={},
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    live = IdempotencyKey(
        user_id=user.id,
        key=f"live-{marker}",
        request_hash="h",
        response_status=201,
        response_body={},
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    db.add_all([expired, live])
    await db.commit()

    summary = await run_sweep()
    assert summary["idem_keys_deleted"] >= 1  # ours at minimum; residue swept too

    gone = (
        await db.execute(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(
                IdempotencyKey.user_id == user.id,
                IdempotencyKey.key == f"expired-{marker}",
            )
        )
    ).scalar_one()
    kept = (
        await db.execute(
            select(func.count())
            .select_from(IdempotencyKey)
            .where(
                IdempotencyKey.user_id == user.id,
                IdempotencyKey.key == f"live-{marker}",
            )
        )
    ).scalar_one()
    assert (gone, kept) == (0, 1)
