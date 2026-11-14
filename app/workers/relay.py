"""Outbox -> Celery relay loop.

Claims unpublished outbox rows in small batches (SELECT ... FOR UPDATE SKIP
LOCKED), sends them to Celery, sets published_at, increments attempts. Safe
to run multiple relay instances concurrently. Phase 6.
May depend on: db.session, repositories.outbox, workers.celery_app.
"""
