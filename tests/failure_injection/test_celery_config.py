"""The redelivery guarantees are CONFIG, not code — pin them here.

A regression in any of these silently turns "worker killed mid-task" from a
redelivery into a LOST task (the file then hangs in uploaded/processing
until the sweeper rescues it — recovery by janitor instead of by design).
"""

from __future__ import annotations

from app.core.config import get_settings
from app.workers.celery_app import celery_app


def test_ack_and_redelivery_settings() -> None:
    conf = celery_app.conf
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.worker_prefetch_multiplier == 1


def test_visibility_timeout_must_exceed_hard_time_limit() -> None:
    """A shorter visibility timeout redelivers a task that is STILL RUNNING
    (duplicate concurrent run); the hard limit must kill it first."""
    conf = celery_app.conf
    soft, hard = conf.task_soft_time_limit, conf.task_time_limit
    settings = get_settings()
    assert soft == settings.TASK_SOFT_TIME_LIMIT
    assert hard == settings.TASK_HARD_TIME_LIMIT
    assert soft < hard, "soft limit must fire before the hard kill"
    assert conf.broker_transport_options["visibility_timeout"] > hard


def test_no_result_backend() -> None:
    """The files row IS the result; a result backend would be a second
    source of truth (and Redis memory for nothing)."""
    conf = celery_app.conf
    assert conf.task_ignore_result is True
    assert conf.result_backend is None


def test_sweep_is_scheduled() -> None:
    schedule = celery_app.conf.beat_schedule
    entry = schedule["sweep-once"]
    assert entry["task"] == "sweep_once"
    assert entry["schedule"] == float(get_settings().SWEEP_INTERVAL_SECONDS)
