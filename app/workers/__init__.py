"""Worker processes (Celery + relay + pipelines). Nothing here runs inside
the API; worker code owns its engine (NullPool) and lifecycle. Skeleton in
Phase 1; Celery app + tasks land in Phase 6, pipelines in Phase 7."""
