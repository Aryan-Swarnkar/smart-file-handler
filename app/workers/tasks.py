"""Celery tasks: process_file drives ONLY the state machine (Phase 6).

Invariants (from the spec + AGENTS.md):
- Conditional transitions (`UPDATE ... WHERE status = ...`): the rowcount is
  the race referee, so duplicate deliveries are no-ops and at-least-once
  delivery is safe.
- Two SEPARATE transactions (uploaded->processing, then processing->done)
  mirror the Phase 7 shape, where real pipeline work runs between them: a
  worker killed between the steps leaves `processing`, which a redelivery
  or the sweeper's rescue re-enqueue then finishes. NOTE for Phase 7: with
  real work between the steps, a duplicate delivery can observe
  `processing` mid-flight — the work must be idempotent or claim-guarded.
- Engine PER task invocation (NullPool), created inside the task body and
  disposed in `finally`: workers are forked processes and must never share
  the API's engine/pool (AGENTS.md rule 8).
- NonRetryableError goes straight to `failed` with a display-safe reason in
  files.error (the API surfaces it — "format: ...", "network: ...").
  Retryable errors retry with exponential backoff + jitter; the LAST
  attempt also lands in `failed`, otherwise a permanently broken file would
  loop through the sweeper's rescue forever.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import FileStatus
from app.repositories.files import FileRepository
from app.workers.celery_app import celery_app

logger = get_logger(__name__)

# files.error is a display column (surfaced by the API) — hard-cap the
# reason written to it.
MAX_ERROR_LENGTH = 500


class NonRetryableError(Exception):
    """Terminal, deterministic failure — retrying cannot succeed (bad kind,
    corrupt object, unsupported payload). `reason` is display-safe text the
    API surfaces via files.error; never embed SQL or internals in it."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _RequestLike(Protocol):
    retries: int


class _TaskLike(Protocol):
    """The slice of celery.Task the task body needs. The body is a plain
    function taking this, so failure-injection tests drive it with a fake —
    no broker, no request machinery."""

    max_retries: int
    request: _RequestLike

    def retry(self, *, exc: BaseException, countdown: float) -> Any: ...


def _build_engine() -> AsyncEngine:
    """Fresh engine per task invocation (NullPool) — never a shared pool."""
    return create_async_engine(get_settings().DATABASE_URL, poolclass=NullPool)


def _retry_countdown(retries: int) -> float:
    """Exponential backoff + jitter: ~1-2s, ~2-3s, ~4-5s ... capped at 60s."""
    return min(2.0**retries, 60.0) + random.uniform(0, 1)


async def _drive_state_machine(file_id: uuid.UUID) -> None:
    """uploaded -> processing -> done, two conditional steps in two
    transactions. Duplicate deliveries no-op at whichever step no longer
    applies; a delivery that finds `processing` FINISHES it (the delivery
    that claimed step 1 may have died before step 2). A missing row or a
    non-participating status is a graceful no-op — stale events must never
    resurrect or fail rows."""
    engine = _build_engine()
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            repo = FileRepository(session)

            # Step 1: uploaded -> processing. 0 rows = not ours to claim.
            claimed = await repo.mark_processing(file_id)
            if claimed:
                await session.commit()  # step 1 committed — the kill seam
            else:
                status = await repo.get_status(file_id)
                if status is None:
                    logger.info("process_file_row_missing", file_id=str(file_id))
                    return
                if status is not FileStatus.PROCESSING:
                    logger.info(
                        "process_file_noop", file_id=str(file_id), status=status.value
                    )
                    return
                await session.rollback()  # end the read tx before step 2

            # Step 2: processing -> done (own tx — the kill-recovery seam).
            done = await repo.mark_done(file_id)
            if done:
                await session.commit()
                logger.info("file_processed", file_id=str(file_id))
            else:
                await session.rollback()
                status = await repo.get_status(file_id)
                logger.info(
                    "process_file_step2_noop",
                    file_id=str(file_id),
                    status=status.value if status is not None else None,
                )
    finally:
        await engine.dispose()


async def _mark_failed(file_id: uuid.UUID, reason: str) -> None:
    """Terminal `failed` write in its own engine/session."""
    engine = _build_engine()
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            rowcount = await FileRepository(session).mark_failed(
                file_id, message=reason[:MAX_ERROR_LENGTH]
            )
            await session.commit()
            if rowcount == 0:
                logger.warning("process_file_failed_mark_no_row", file_id=str(file_id))
    finally:
        await engine.dispose()


def _terminal_failed(task: _TaskLike, file_id: uuid.UUID, reason: str) -> None:
    """Best-effort `failed` write. If the write itself fails (DB down), the
    row keeps its status: with retries left the task retries (the next run
    re-drives the machine); exhausted, a CRITICAL log and the sweeper's
    rescue gives the write another chance later."""
    try:
        asyncio.run(_mark_failed(file_id, reason))
    except Exception as mark_exc:
        if task.request.retries < task.max_retries:
            raise task.retry(
                exc=mark_exc, countdown=_retry_countdown(task.request.retries)
            ) from mark_exc
        logger.critical(
            "process_file_failed_mark_also_failed",
            file_id=str(file_id),
            reason=reason[:200],
            error=str(mark_exc)[:200],
        )
        return
    logger.error(
        "process_file_terminal_failed", file_id=str(file_id), reason=reason[:200]
    )


def _process_file_body(task: _TaskLike, file_id: str) -> None:
    """Task body, isolated from Celery plumbing. Outcomes: success (row
    `done` or a justified no-op), a celery retry, or terminal `failed` with
    the reason in files.error."""
    try:
        fid = uuid.UUID(file_id)
    except ValueError:
        # Poison message: no valid id -> no row exists to fail. The relay
        # dead-letters malformed payloads before they get here; this is the
        # last-line log.
        logger.error("process_file_bad_file_id", file_id=file_id[:64])
        return

    reason: str | None = None
    try:
        asyncio.run(_drive_state_machine(fid))
    except NonRetryableError as exc:
        reason = exc.reason
    except Exception as exc:
        if task.request.retries < task.max_retries:
            countdown = _retry_countdown(task.request.retries)
            logger.warning(
                "process_file_retrying",
                file_id=str(fid),
                retries=task.request.retries,
                countdown=round(countdown, 2),
                error_type=type(exc).__name__,
                error=str(exc)[:200],
            )
            raise task.retry(exc=exc, countdown=countdown) from exc
        reason = (
            f"processing failed after {task.request.retries + 1} attempts: "
            f"{type(exc).__name__}: {exc}"
        )

    if reason is not None:
        _terminal_failed(task, fid, reason)


@celery_app.task(
    bind=True,
    name="process_file",
    max_retries=get_settings().CELERY_TASK_MAX_RETRIES,
)
def process_file(self: Any, file_id: str) -> None:
    """Drive ONLY the state machine for one file. The relay queues it for
    `file.uploaded` and `file.reprocess`; scan/label/text pipelines hook in
    between the two transitions in Phase 7."""
    _process_file_body(self, file_id)
