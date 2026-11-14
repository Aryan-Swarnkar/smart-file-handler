"""Celery tasks: process_file drives ONLY the state machine.

Transitions use conditional UPDATE ... WHERE status IN (...) so duplicate
deliveries are no-ops (idempotent). Each task body runs via asyncio.run(...)
with an engine created inside the task using NullPool, reusing the same async
repositories. Retry: exponential backoff + jitter, max retries, and a
NonRetryableError class that goes straight to `failed` with an error message.
Phase 6. May depend on: db.session, repositories, pipelines.
"""
