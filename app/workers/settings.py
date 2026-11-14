"""ARQ WorkerSettings — the `worker` Docker target runs:
`arq app.workers.settings.WorkerSettings`.

Startup/shutdown mirror the API's lifespan: one session factory + one
StorageService per worker process, stashed in the worker `ctx` so the job
coroutines in `pipeline.py` stay pure business logic. `retry_jobs=True`
(arq default) re-queues jobs whose worker died mid-run — the second half of
crash recovery (the sweep is the first half).
"""

from __future__ import annotations

from typing import Any, ClassVar

from arq.connections import RedisSettings
from arq.cron import CronJob, cron
from arq.typing import WorkerCoroutine

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import session_factory
from app.services.storage import StorageService
from app.workers.pipeline import process_file, sweep_uploads

logger = get_logger(__name__)


def _cron_seconds() -> set[int]:
    """Quantize the sweep interval onto the cron second grid (arq crons tick
    on minute boundaries). 30s → {0,30}; 15s → {0,15,30,45}; ≥60 → {0}."""
    interval = get_settings().SWEEP_INTERVAL_SECONDS
    return {0} if interval >= 60 else set(range(0, 60, max(1, interval)))


async def on_startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    ctx["settings"] = settings
    ctx["session_factory"] = session_factory
    # The P5/P6 pipelines read/write S3 objects; start the client now so
    # plugging them in needs zero changes to this file.
    storage = StorageService()
    await storage.start()
    ctx["storage"] = storage
    logger.info(
        "worker_started",
        env=settings.ENV,
        sweep_seconds=settings.SWEEP_INTERVAL_SECONDS,
        max_tries=settings.WORKER_MAX_TRIES,
    )


async def on_shutdown(ctx: dict[str, Any]) -> None:
    storage: StorageService | None = ctx.get("storage")
    if storage is not None:
        await storage.close()
    logger.info("worker_stopped")


class WorkerSettings:
    """Fields are read by the arq CLI (not instantiated by us)."""

    functions: ClassVar[list[WorkerCoroutine]] = [process_file, sweep_uploads]
    cron_jobs: ClassVar[list[CronJob]] = [
        cron(
            sweep_uploads,
            second=_cron_seconds(),
            run_at_startup=True,
            unique=True,
            keep_result=0,
        )
    ]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
    max_tries = get_settings().WORKER_MAX_TRIES
    max_jobs = 10
    job_timeout = 300  # per-attempt; pipelines get 5 minutes in P5/P6 too
