"""Periodic sweeps (Celery beat, every SWEEP_INTERVAL_SECONDS).

Three clauses, each independently idempotent and independently retried (a
failure in one never blocks the others — each commits its own transactions):

1. Rescue stuck files: uploaded/processing rows past STUCK_FILE_MINUTES are
   re-enqueued via an OUTBOX row (file.reprocess) — NEVER a direct Celery
   enqueue: the outbox stays the only bridge from DB state to Celery and
   the relay stays the single publisher (AGENTS.md rule 4). Deduped: while
   the worker is down, ONE unpublished reprocess per file is enough (the
   relay republishes it until it is delivered).
2. Expire pending_upload rows past upload_expires_at + grace: delete the
   object FIRST (idempotent; S3 outside the transaction so an S3 failure
   mutates nothing), then a conditional DELETE whose rowcount is the race
   referee against a concurrent /complete or /delete, then release the
   owner's quota in the SAME transaction. Rowcount 0 after a successful
   object delete = a concurrent completer/deleter won in that instant —
   CRITICAL log (the pathological complete-in-grace-window interleave).
3. Purge expired idempotency_keys (the model docstring assigns this here;
   without it the replay table grows unbounded).

Trash purge (30 days) is Phase 10 — deliberately absent here.
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.logging import get_logger
from app.repositories.files import ExpiredPending, FileRepository, StuckFile
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.quota import QuotaRepository
from app.services.storage_service import StorageService
from app.workers.celery_app import celery_app
from app.workers.pause import pipeline_clear

logger = get_logger(__name__)


async def _rescue_stuck_files(factory: async_sessionmaker[AsyncSession]) -> int:
    """Re-enqueue stuck uploaded/processing files via outbox rows."""
    settings = get_settings()
    async with factory() as session:
        stuck: list[StuckFile] = await FileRepository(session).select_stuck(
            minutes=settings.STUCK_FILE_MINUTES
        )

    rescued = 0
    for row in stuck:
        async with factory() as session:
            outbox = OutboxRepository(session)
            if await outbox.has_unpublished_event(
                event_type="file.reprocess", file_id=row.id
            ):
                await session.rollback()
                continue  # one unpublished reprocess per file is enough
            await outbox.insert_event(
                event_type="file.reprocess",
                payload={"file_id": str(row.id), "owner_id": str(row.user_id)},
            )
            await session.commit()
            rescued += 1
    if rescued:
        logger.info("sweep_rescued_stuck_files", count=rescued)
    return rescued


async def _expire_pending_uploads(factory: async_sessionmaker[AsyncSession]) -> int:
    """Reap abandoned pending uploads: object first, then row + quota."""
    settings = get_settings()
    async with factory() as session:
        candidates: list[ExpiredPending] = await FileRepository(
            session
        ).select_expired_pending(grace_seconds=settings.PENDING_SWEEP_GRACE_SECONDS)
    if not candidates:
        return 0

    storage = StorageService()
    await storage.start()
    reaped = 0
    try:
        for row in candidates:
            try:
                # Idempotent; a missing object is a no-op. S3 outside the tx:
                # if this fails, NOTHING was mutated — next sweep retries.
                await storage.delete(row.storage_key)
            except Exception as exc:
                logger.warning(
                    "sweep_object_delete_failed",
                    file_id=str(row.id),
                    error=str(exc)[:200],
                )
                continue

            async with factory() as session:
                deleted = await FileRepository(session).delete_expired_pending(
                    row.id, grace_seconds=settings.PENDING_SWEEP_GRACE_SECONDS
                )
                if not deleted:
                    # We deleted the object, but the row left expired-pending
                    # state before our DELETE: a concurrent complete/delete
                    # won the race. CRITICAL — the completed file's object is
                    # now missing (accepted residual, logged for operators).
                    await session.rollback()
                    logger.critical(
                        "sweep_reap_lost_race",
                        file_id=str(row.id),
                        detail="object deleted but row is no longer expired-pending",
                    )
                    continue
                released = await QuotaRepository(session).release(
                    row.user_id, row.size_bytes
                )
                await session.commit()
                if not released:
                    # Defensive: the reservation should still hold the
                    # counter. Log loudly — accounting must never drift.
                    logger.warning(
                        "sweep_quota_release_guarded",
                        file_id=str(row.id),
                        owner_id=str(row.user_id),
                        size_bytes=row.size_bytes,
                    )
                reaped += 1
        if reaped:
            logger.info("sweep_expired_pending_reaped", count=reaped)
        return reaped
    finally:
        await storage.close()


async def _delete_expired_idempotency_keys(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    async with factory() as session:
        deleted = await IdempotencyRepository(session).delete_expired()
        await session.commit()
    if deleted:
        logger.info("sweep_idempotency_keys_deleted", count=deleted)
    return deleted


async def _sweep() -> dict[str, int]:
    engine: AsyncEngine = create_async_engine(
        get_settings().DATABASE_URL, poolclass=NullPool
    )
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        return {
            "rescued": await _rescue_stuck_files(factory),
            "expired_reaped": await _expire_pending_uploads(factory),
            "idem_keys_deleted": await _delete_expired_idempotency_keys(factory),
        }
    finally:
        await engine.dispose()


@celery_app.task(name="sweep_once")
def sweep_once() -> str:
    """Run one sweep — skipped while the pipeline-pause lock is held
    (workers/pause.py). Returns a JSON summary (handy for eager test runs;
    the broker stores nothing — task_ignore_result=True)."""
    return asyncio.run(_sweep_task())


async def _sweep_task() -> str:
    if not await pipeline_clear():
        logger.info("sweep_skipped_paused")
        return json.dumps({"skipped": "pipeline_paused"})
    return json.dumps(await _sweep())
