"""ARQ worker jobs: file processing pipeline + crash-recovery sweep.

Layering: these functions ARE the service layer for the worker process —
they own business rules and commits, and delegate SQL to repositories.

Idempotency contract (user-locked P4):
- Claim is a status transition `uploaded → processing`; a job that runs again
  (retry after a mid-job crash, duplicate enqueue) skips anything already
  claimed/done — a file is never processed twice.
- The API enqueues with job_id `process:{file_id}`; ARQ collapses duplicate
  enqueues of the same job_id while the job exists in the queue.
- `sweep_uploads` re-enqueues unclaimed completed rows every interval, so an
  API that died between its DB commit and its Redis enqueue still gets picked
  up. Files already done/failed are ignored by the claim guard.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from arq.worker import Retry
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import File, FileStatus
from app.db.session import session_factory
from app.repositories.files import FileRepository

logger = get_logger(__name__)

PROCESS_JOB_NAME = "process_file"
PROCESS_JOB_PREFIX = "process:"


def _backoff_seconds(tries_done: int) -> float:
    """Exponential backoff for attempt N: 5s, 10s, 20s… capped."""
    s = get_settings()
    return min(
        float(s.WORKER_RETRY_BASE_SECONDS) * (2 ** (tries_done - 1)),
        float(s.WORKER_RETRY_MAX_SECONDS),
    )


def _session_factory(ctx: dict) -> async_sessionmaker:
    """Worker-created session factory, with the direct-call test path falling
    back to the module-level one."""
    factory = ctx.get("session_factory")
    if factory is None:
        return session_factory
    return factory


async def _run_pipeline(file: File) -> None:
    """The pipeline seam — P4 skeleton is a deliberate no-op.

    P5 (faces/CLIP) and P6 (pdf/office/text) replace the body of this function
    and NOTHING else: claim/finish/error bookkeeping below stays identical.
    doc_topic stays NULL for processable kinds until P6 assigns real topics
    (the `other` topic is Matrix A, set at complete time).
    """
    await asyncio.sleep(0)


async def process_file(ctx: dict, file_id: str) -> str:
    r"""Claim → pipeline → done, with idempotency + retry bookkeeping.

    Status transitions handled here (never by the API):
      uploaded --claim--> processing --pipeline--> done
                                            \--> failed (after max tries)
    """
    fid = uuid.UUID(file_id)
    settings = get_settings()
    factory = _session_factory(ctx)

    async with factory() as session:
        repo = FileRepository(session)
        file = await repo.get_by_id(fid)

        if file is None:
            # Deleted before we got here (e.g. user hard-delete during a
            # retry backoff) — not an error, nothing to do.
            logger.info("process_file_missing", file_id=file_id)
            return "missing"

        if file.sha256 is None:
            # Defensive: an init row can never be processed (no bytes).
            logger.warning(
                "process_file_incomplete", file_id=file_id
            )
            return "incomplete"

        if file.status is FileStatus.UPLOADED:
            # Claim. If the worker dies mid-job, ARQ's own retry re-runs the
            # job and this branch is skipped (already claimed) — re-entry
            # into a claimed file is safe because the skeleton is idempotent.
            file.status = FileStatus.PROCESSING
            await session.commit()
            logger.info("file_claimed", file_id=file_id)
        elif file.status in (FileStatus.DONE, FileStatus.FAILED):
            return "skipped"
        # status == PROCESSING → already claimed by an earlier attempt; fall
        # through and (re)run the pipeline.

        try:
            await _run_pipeline(file)
        except Exception as exc:
            tries_done = int(ctx.get("job_try") or 1)
            if tries_done >= settings.WORKER_MAX_TRIES:
                # Terminal: surface the failure on the row (browsable, and
                # the UI shows it as the failed chip).
                file.status = FileStatus.FAILED
                file.error = str(exc)[:500]
                await session.commit()
                logger.error(
                    "file_failed_terminal",
                    file_id=file_id,
                    tries=tries_done,
                    error=str(exc),
                )
                return "failed"
            logger.warning(
                "file_retry_scheduled",
                file_id=file_id,
                tries_done=tries_done,
                defer=_backoff_seconds(tries_done),
                error=str(exc),
            )
            raise Retry(defer=_backoff_seconds(tries_done)) from exc

        file.status = FileStatus.DONE
        file.error = None
        await session.commit()
        logger.info("file_processed", file_id=file_id)
        return "done"


async def sweep_uploads(ctx: dict) -> str:
    """Crash-recovery sweep (cron, user-locked: enqueue + sweep).

    1. Completed-but-unclaimed rows (status=uploaded, sha256 set) → enqueue
       `process_file` idempotently (job_id collapses duplicates).
    2. Orphan init rows (sha256 still NULL, older than the grace window —
       presigned URLs are long dead) → deleted; no S3 cleanup is needed
       because without a PUT there is no object.
    """
    settings = get_settings()
    factory = _session_factory(ctx)
    arq_redis = ctx.get("redis")
    now = datetime.now(UTC)
    enqueued, deleted = 0, 0

    async with factory() as session:
        repo = FileRepository(session)

        if arq_redis is not None:
            candidates = await repo.list_enqueue_candidates()
            for file in candidates:
                job = await arq_redis.enqueue_job(
                    "process_file",
                    str(file.id),
                    _job_id=f"{PROCESS_JOB_PREFIX}{file.id}",
                )
                if job is not None:
                    enqueued += 1
            await session.commit()

        older_than = now - timedelta(minutes=settings.ORPHAN_INIT_MINUTES)
        deleted = await repo.delete_orphan_inits(older_than=older_than)
        await session.commit()

    if enqueued or deleted:
        logger.info("sweep", enqueued=enqueued, orphans_deleted=deleted)
    return f"enqueued={enqueued} orphans_deleted={deleted}"
