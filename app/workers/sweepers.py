"""Periodic sweeps (Celery beat / periodic task).

Re-enqueue files stuck in uploaded/processing past a timeout; expire
pending_upload rows (delete the object if it exists, release quota); purge
trash older than 30 days (Phase 10). Nothing else. Phase 6+.
May depend on: repositories, workers.celery_app.
"""
