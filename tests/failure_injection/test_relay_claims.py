"""Relay claim/publish/dead-letter behavior under injected failures.

The broker is a fake (no Redis in the loop): what's proven here is the
relay's OWN contract — disjoint claims across instances, duplicate-tolerant
publish, attempts committed even when the broker is down, and poison rows
dead-lettered by log instead of churning forever.
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FileStatus, Outbox
from app.repositories.files import FileRepository
from app.repositories.outbox import OutboxRepository
from app.workers import relay as relay_module
from app.workers import tasks as tasks_module
from tests.failure_injection.conftest import (
    FakeBroker,
    make_factory,
    seed_file,
    seed_outbox_event,
    seed_user_with_quota,
)


async def status_of(db: AsyncSession, file_id: uuid.UUID) -> FileStatus | None:
    """Scalar read — immune to identity-map staleness across engines."""
    return await FileRepository(db).get_status(file_id)


async def published_count(db: AsyncSession) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(Outbox)
            .where(Outbox.published_at.is_not(None))
        )
    ).scalar_one()


# --- multiple relays -----------------------------------------------------------------


async def test_two_relays_claim_disjoint_sets(db: AsyncSession) -> None:
    claimed_ids: set[int] = set()
    for _ in range(5):
        row = await seed_outbox_event(db)
        claimed_ids.add(row.id)
    await db.commit()

    e1, f1 = make_factory()
    e2, f2 = make_factory()
    try:
        async with f1() as s1, f2() as s2:
            claims_1, claims_2 = await asyncio.gather(
                OutboxRepository(s1).claim_unpublished(limit=3),
                OutboxRepository(s2).claim_unpublished(limit=3),
            )
            await s1.commit()
            await s2.commit()
    finally:
        await e1.dispose()
        await e2.dispose()

    ids_1 = {c.id for c in claims_1}
    ids_2 = {c.id for c in claims_2}
    assert not (ids_1 & ids_2), "SKIP LOCKED must hand out disjoint sets"
    assert ids_1 | ids_2 == claimed_ids, "every row must be claimed exactly once"
    assert all(c.attempts == 1 for c in (*claims_1, *claims_2))


async def test_crash_between_send_and_stamp_republishes_safely(
    db: AsyncSession,
) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(db, user, status=FileStatus.UPLOADED)
    row = await seed_outbox_event(db, event_type="file.uploaded", file_id=file.id)
    await db.commit()

    engine, factory = make_factory()
    try:
        # Cycle 1: claimed (+attempts), sent... then the relay "crashes"
        # before stamping.
        async with factory() as session:
            claims = await OutboxRepository(session).claim_unpublished(limit=10)
            await session.commit()
        assert len(claims) == 1 and claims[0].attempts == 1
        # (the send itself happened — the broker now has the task)

        # Cycle 2 (relay restarted): the row is re-claimable.
        async with factory() as session:
            reclaims = await OutboxRepository(session).claim_unpublished(limit=10)
            await session.commit()
        assert [c.id for c in reclaims] == [row.id]
        assert reclaims[0].attempts == 2, "attempts counts claim cycles"
        # ...and this time the stamp lands.
        async with factory() as session:
            stamped = await OutboxRepository(session).stamp_published([row.id])
            await session.commit()
        assert stamped == 1
    finally:
        await engine.dispose()

    # Both deliveries ran the task: idempotent, exactly one outcome.
    await tasks_module._drive_state_machine(file.id)
    await tasks_module._drive_state_machine(file.id)
    assert (
        await db.execute(select(Outbox.published_at).where(Outbox.id == row.id))
    ).scalar_one() is not None
    assert await status_of(db, file.id) is FileStatus.DONE


# --- mapping + dead-lettering --------------------------------------------------------


async def test_valid_events_sent_and_poison_rows_dead_lettered(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    f1 = await seed_file(db, user, status=FileStatus.UPLOADED)
    f2 = await seed_file(db, user, status=FileStatus.UPLOADED)
    row_upload = await seed_outbox_event(db, event_type="file.uploaded", file_id=f1.id)
    row_repro = await seed_outbox_event(db, event_type="file.reprocess", file_id=f2.id)
    row_unknown = await seed_outbox_event(db, event_type="folder.purge")  # unmapped
    row_malformed = await seed_outbox_event(db, event_type="file.uploaded")
    row_malformed.payload = {}  # no file_id -> poison
    await db.commit()

    broker = FakeBroker()
    engine, _ = make_factory()
    try:
        published = await relay_module._cycle(engine, broker)
    finally:
        await engine.dispose()

    assert published == 4
    assert set(broker.sent) == {
        ("process_file", str(f1.id)),
        ("process_file", str(f2.id)),
    }
    assert await published_count(db) == 4  # poison rows stamped, not churned
    for row_id in (row_upload.id, row_repro.id, row_unknown.id, row_malformed.id):
        stamped = (
            await db.execute(select(Outbox.published_at).where(Outbox.id == row_id))
        ).scalar_one()
        assert stamped is not None


async def test_broker_down_increments_attempts_without_crash(db: AsyncSession) -> None:
    row_1 = await seed_outbox_event(db)
    row_2 = await seed_outbox_event(db)
    await db.commit()

    broker = FakeBroker(fail=ConnectionError("redis is down"))
    engine, _ = make_factory()
    try:
        # Both cycles must not raise — the loop survives broker downtime.
        published = await relay_module._cycle(engine, broker)
        assert published == 0
        await relay_module._cycle(engine, broker)
    finally:
        await engine.dispose()

    for row_id in (row_1.id, row_2.id):
        attempt, published_at = (
            await db.execute(
                select(Outbox.attempts, Outbox.published_at).where(Outbox.id == row_id)
            )
        ).one()
        assert attempt == 2, "attempts committed even though sends failed"
        assert published_at is None, "an unpublished row stays republishable"
