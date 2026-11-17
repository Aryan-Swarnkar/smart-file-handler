"""Outbox -> Celery relay loop.

One claim->send->stamp cycle per poll, made crash-safe:

1. claim_unpublished(): SELECT ... FOR UPDATE SKIP LOCKED small batch +
   attempts++, committed BEFORE any broker I/O — row locks never span a
   network call, and a crash here costs only an attempts tick.
2. send each claimed row to Celery via asyncio.to_thread (kombu is sync;
   the relay's event loop must never block on it — AGENTS.md rule 8). The
   first hard send failure aborts the rest of the batch: the broker is
   almost certainly down and the remaining sends would fail identically.
3. stamp_published(ids): the crash window between 2 and 3 republishes the
   row later — a duplicate delivery, made harmless by idempotent tasks.

Safe to run multiple relay instances: SKIP LOCKED hands each claim a
disjoint set at that instant, and any overlap beyond it is just another
duplicate delivery.

No attempts cap, deliberately: the only realistic send failure is broker
downtime, and a cap would strand EVERY unpublished row after a long outage
(manual recovery is strictly worse than unbounded retries). Log severity
escalates instead (WARNING < 20 attempts, ERROR from then on).

Dead-lettering by log: unknown event_type and payloads without a file_id
are stamped published so they stop churning every poll; the ERROR log is
the operator signal, the outbox row keeps the payload for forensics.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from celery import Celery
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.repositories.outbox import OutboxClaim, OutboxRepository
from app.workers.celery_app import celery_app
from app.workers.pause import pipeline_clear

logger = get_logger(__name__)

# The relay's whole task-mapping table. New event types (Phase 7 pipeline
# stages, Phase 10 storage deletes) extend it; anything unmapped is
# dead-lettered, never silently dropped.
EVENT_TO_TASK: dict[str, str] = {
    "file.uploaded": "process_file",
    "file.reprocess": "process_file",
}


def _log_send_failure(claim: OutboxClaim, exc: Exception) -> None:
    """Severity escalates with attempts (no cap -> escalating logs instead)."""
    log = logger.warning if claim.attempts < 20 else logger.error
    log(
        "relay_send_failed",
        outbox_id=claim.id,
        event_type=claim.event_type,
        attempts=claim.attempts,
        error=str(exc)[:200],
    )


async def _cycle(engine: AsyncEngine, broker: Celery) -> int:
    """One pass. Returns the number of rows published (stamped)."""
    settings = get_settings()
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # 1. Claim + attempts++ committed before ANY broker I/O.
    async with factory() as session:
        claims = await OutboxRepository(session).claim_unpublished(
            limit=settings.RELAY_BATCH_SIZE
        )
        await session.commit()
    if not claims:
        return 0

    published_ids: list[int] = []
    for claim in claims:
        task_name = EVENT_TO_TASK.get(claim.event_type)
        if task_name is None:
            logger.error(
                "relay_unknown_event_type",
                outbox_id=claim.id,
                event_type=claim.event_type,
            )
            published_ids.append(claim.id)  # dead-letter by log: stop the churn
            continue
        try:
            file_id = str(claim.payload["file_id"])
        except (KeyError, TypeError):
            logger.error(
                "relay_malformed_payload",
                outbox_id=claim.id,
                event_type=claim.event_type,
            )
            published_ids.append(claim.id)
            continue
        try:
            # 2. kombu is synchronous — never block the loop on it.
            await asyncio.to_thread(broker.send_task, task_name, kwargs={"file_id": file_id})
        except Exception as exc:
            _log_send_failure(claim, exc)
            break  # broker down: the rest of the batch would fail identically
        published_ids.append(claim.id)

    if not published_ids:
        return 0
    # 3. Stamp what was sent. Crash between 2 and 3 -> republish (duplicate).
    async with factory() as session:
        stamped = await OutboxRepository(session).stamp_published(published_ids)
        await session.commit()
    return stamped


async def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.LOG_LEVEL, log_format=settings.LOG_FORMAT)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # non-POSIX dev runs
            loop.add_signal_handler(sig, stop.set)

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    logger.info(
        "relay_started",
        poll_seconds=settings.RELAY_POLL_SECONDS,
        batch_size=settings.RELAY_BATCH_SIZE,
    )
    try:
        while not stop.is_set():
            try:
                # Pause gate: a session-level advisory lock holder (operator
                # maintenance, or the test harness — see workers/pause.py)
                # stops NEW claims; in-flight cycles finish normally.
                if await pipeline_clear():
                    await _cycle(engine, celery_app)
                else:
                    logger.debug("relay_paused")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("relay_cycle_failed")
            try:
                # Sleep that doubles as a responsive shutdown wait.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(), timeout=settings.RELAY_POLL_SECONDS
                    )
            except asyncio.CancelledError:
                raise
    finally:
        await engine.dispose()
        logger.info("relay_stopped")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):  # interactive dev runs
        asyncio.run(main())
