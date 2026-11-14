"""Celery application (Redis broker).

Config: acks_late=True, reject_on_worker_lost=True, worker_prefetch_multiplier=1,
soft/hard time limits, visibility_timeout larger than the longest task;
concurrency 1 for the ML worker. Phase 6.
May depend on: core.config.
"""
