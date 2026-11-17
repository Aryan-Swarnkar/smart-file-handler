"""Celery application (Redis broker).

Config locked by the Phase 6 prompt: acks_late + reject_on_worker_lost so a
killed worker's in-flight task is redelivered, prefetch 1 so a slow task
never starves the queue behind it, soft/hard time limits with a broker
visibility timeout STRICTLY larger than the hard limit (a running task must
never be redelivered mid-flight), and no result backend — the `files` row IS
the result. Concurrency 1 for the ML worker is set by the worker COMMAND in
compose, not here: the app must not hard-code deployment shape.

Task names are pinned short ("process_file", "sweep_once") — the relay maps
outbox event_type -> task name, and the beat schedule references the sweep.
"""

from __future__ import annotations

from celery import Celery

from app.core.config import get_settings


def make_celery_app() -> Celery:
    settings = get_settings()
    app = Celery(
        "smart_file_handler",
        broker=settings.REDIS_URL,
        include=["app.workers.tasks", "app.workers.sweepers"],
    )
    app.conf.update(
        # A task is acked only AFTER it finishes; a worker that dies (OOM,
        # deploy, hard time limit) loses the ack, so the broker redelivers.
        task_acks_late=True,
        # A worker losing contact mid-task (kill -9, OOM) rejects instead of
        # acking — the task is redelivered rather than vanishing.
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        # No result backend: process_file's outcome is the files row; the
        # sweeper's summary is a log line. Nothing reads task results.
        task_ignore_result=True,
        task_serializer="json",
        accept_content=["json"],
        # Soft limit raises inside the task (goes down the retryable path);
        # hard limit kills the process (becomes a redelivery via the two
        # settings above). Hard must exceed soft.
        task_soft_time_limit=settings.TASK_SOFT_TIME_LIMIT,
        task_time_limit=settings.TASK_HARD_TIME_LIMIT,
        # Redis unacknowledged-task requeue window. MUST exceed the hard
        # limit or the broker redelivers a task that is still running.
        broker_transport_options={
            "visibility_timeout": settings.BROKER_VISIBILITY_TIMEOUT
        },
        # A worker booting before redis is ready retries instead of dying.
        broker_connection_retry_on_startup=True,
        timezone="UTC",
        enable_utc=True,
    )
    app.conf.beat_schedule = {
        "sweep-once": {
            "task": "sweep_once",
            "schedule": float(settings.SWEEP_INTERVAL_SECONDS),
        },
    }
    return app


celery_app = make_celery_app()
