"""Celery tasks: process_file = state machine + real pipelines (Phase 7).

Invariants (from the spec + AGENTS.md):
- Conditional transitions (`UPDATE ... WHERE status = ...`): the rowcount is
  the race referee, so duplicate deliveries are no-ops and at-least-once
  delivery is safe.
- Two SEPARATE transactions: step 1 claims uploaded->processing and reads
  the pipeline inputs; the PIPELINE (download+hash, ClamAV scan, kind
  dispatch) runs with NO transaction held; step 2 marks processing->done.
  A worker killed between the steps leaves `processing`, which a redelivery
  or the sweeper's rescue re-enqueue then finishes (the pipeline is
  idempotent, so the finishing delivery simply redoes the work).
- Engine PER task invocation (NullPool), created inside the task body and
  disposed in `finally`: workers are forked processes and must never share
  the API's engine/pool (AGENTS.md rule 8).
- NonRetryableError goes straight to `failed` with a display-safe reason in
  files.error (the API surfaces it — "format: ...", "network: ...").
  Retryable errors retry with exponential backoff + jitter; the LAST
  attempt also lands in `failed`, otherwise a permanently broken file would
  loop through the sweeper's rescue forever.
- An infected verdict quarantines in ONE transaction: processing->
  quarantined, quota released, and the storage.delete outbox row (the relay
  then removes the object; the API never touches Celery — rule 4).
"""

from __future__ import annotations

import asyncio
import random
import tempfile
import uuid
from pathlib import Path
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
from app.db.models import FileKind, FileStatus
from app.repositories.files import FileFacts, FileRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.quota import QuotaRepository
from app.services.storage_service import StorageService
from app.workers.celery_app import celery_app
from app.workers.errors import NonRetryableError  # re-exported for tests/FI:

# tasks_module.NonRetryableError stays the task-facing name; pipelines
# import the same class from app.workers.errors (no import cycle).
from app.workers.pipelines import image_pipeline, pdf_pipeline
from app.workers.pipelines.scan import assert_scannable_size, scan_file

logger = get_logger(__name__)

# files.error is a display column (surfaced by the API) — hard-cap the
# reason written to it.
MAX_ERROR_LENGTH = 500


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
    """uploaded -> processing -> [pipeline] -> done. Duplicate deliveries
    no-op at whichever step no longer applies; a delivery that finds
    `processing` FINISHES it (the delivery that claimed step 1 may have died
    mid-pipeline — the pipeline is idempotent, so redoing it is safe). A
    missing row or a non-participating status is a graceful no-op — stale
    events must never resurrect or fail rows."""
    engine = _build_engine()
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        # Step 1: uploaded -> processing, and read the pipeline inputs, in
        # ONE transaction. 0 rows = not ours to claim (or the kill seam).
        async with factory() as session:
            repo = FileRepository(session)
            claimed = await repo.mark_processing(file_id)
            if claimed:
                facts = await repo.get_facts(file_id)
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
                # Kill seam: a previous delivery claimed step 1 and died
                # mid-pipeline. Its facts are still valid — finish the work.
                facts = await repo.get_facts(file_id)
                await session.rollback()  # end the read tx before the pipeline
            if facts is None:
                # Claimed, then the row vanished (concurrent delete): the
                # claim is moot — nothing exists to process or fail.
                logger.warning("process_file_facts_missing", file_id=str(file_id))
                return

        # Pipeline (NO transaction held — short sessions only):
        # download+hash -> size guard -> ClamAV scan -> kind dispatch.
        await _run_pipeline(factory, file_id, facts)

        # Step 2: processing -> done (own tx — the kill-recovery seam).
        async with factory() as session:
            done = await FileRepository(session).mark_done(file_id)
            if done:
                await session.commit()
                logger.info("file_processed", file_id=str(file_id))
            else:
                await session.rollback()
                status = await FileRepository(session).get_status(file_id)
                logger.info(
                    "process_file_step2_noop",
                    file_id=str(file_id),
                    status=status.value if status is not None else None,
                )
    finally:
        await engine.dispose()


async def _run_pipeline(
    factory: async_sessionmaker[AsyncSession],
    file_id: uuid.UUID,
    facts: FileFacts,
) -> None:
    """Download + hash, size guard, ClamAV scan, kind dispatch. Raises
    NonRetryableError for deterministic failures; ScanUnavailable (clamd
    unreachable / unclean answer) propagates as RETRYABLE. An infected
    verdict short-circuits via _quarantine and this function returns."""
    with tempfile.TemporaryDirectory(prefix="sfh-task-") as tmp:
        path = Path(tmp) / "object.bin"
        storage = StorageService()
        await storage.start()
        try:
            digest = await storage.download_to_path(facts.storage_key, path)
        finally:
            await storage.close()
        if digest is None:
            raise NonRetryableError("storage: object missing")
        # Integrity: /complete already stored the real hash — a mismatch
        # means the bytes changed under us (deterministic, never quarantined).
        if facts.sha256 is not None and digest != facts.sha256:
            raise NonRetryableError("integrity: sha256 mismatch")

        assert_scannable_size(facts.size_bytes)
        # ScanUnavailable propagates as RETRYABLE (never mark clean on a
        # maybe); every other scan failure is deterministic.
        verdict = await scan_file(path)
        if verdict.infected:
            await _quarantine(
                factory,
                file_id,
                user_id=facts.user_id,
                size_bytes=facts.size_bytes,
                storage_key=facts.storage_key,
            )
            return

        match facts.kind:
            case FileKind.IMAGE:
                await image_pipeline.run_image_pipeline(
                    factory, file_id=file_id, user_id=facts.user_id, path=path
                )
            case FileKind.PDF:
                await pdf_pipeline.run_pdf_pipeline(
                    factory, file_id=file_id, user_id=facts.user_id, path=path
                )
            case _:
                # other (and legacy kind-NULL rows): scan-only.
                logger.info(
                    "pipeline_scan_only",
                    file_id=str(file_id),
                    kind=facts.kind.value if facts.kind is not None else None,
                )


async def _quarantine(
    factory: async_sessionmaker[AsyncSession],
    file_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    size_bytes: int,
    storage_key: str,
) -> None:
    """Infected verdict -> quarantine in ONE transaction: the conditional
    processing->quarantined UPDATE (rowcount = race referee), the owner's
    quota release, and the storage.delete outbox row (relay removes the
    object; the object itself is never touched inside the DB tx). A lost
    race (the row moved elsewhere meanwhile) writes nothing — the winner
    owns the outcome."""
    async with factory() as session:
        moved = await FileRepository(session).mark_quarantined(file_id)
        if not moved:
            await session.rollback()
            logger.warning("process_file_quarantine_lost_race", file_id=str(file_id))
            return
        released = await QuotaRepository(session).release(user_id, size_bytes)
        if not released:
            # Defensive (accounting must never drift): the reservation should
            # still hold. Mirror the sweeper's guarded-release log.
            logger.warning(
                "process_file_quota_release_guarded",
                file_id=str(file_id),
                user_id=str(user_id),
                size_bytes=size_bytes,
            )
        await OutboxRepository(session).insert_event(
            event_type="storage.delete",
            payload={"storage_key": storage_key, "file_id": str(file_id)},
        )
        await session.commit()
    logger.warning(
        "file_quarantined", file_id=str(file_id), storage_key=storage_key
    )


async def _delete_object(storage_key: str) -> None:
    """Idempotent object removal (S3 delete of a missing key succeeds)."""
    storage = StorageService()
    await storage.start()
    try:
        await storage.delete(storage_key)
    finally:
        await storage.close()


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
    """Drive one file through claim -> pipeline (download+hash, ClamAV scan,
    kind pipelines) -> done. The relay queues it for `file.uploaded` and
    `file.reprocess`."""
    _process_file_body(self, file_id)


@celery_app.task(name="delete_object")
def delete_object(storage_key: str) -> None:
    """Remove one storage object — the worker side of `storage.delete`
    outbox rows (quarantine, and Phase 10's batched deletes). Idempotent:
    a missing object is a no-op, so duplicate deliveries are harmless."""
    if not isinstance(storage_key, str) or not storage_key:
        # Poison message last-line defense: the relay dead-letters malformed
        # payloads before they get here; log loudly and stop.
        logger.error("delete_object_bad_storage_key", key=str(storage_key)[:100])
        return
    asyncio.run(_delete_object(storage_key))
    logger.info("object_deleted", storage_key=storage_key)
