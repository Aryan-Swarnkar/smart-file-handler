"""ClamAV scan pipeline (clamd INSTREAM).

Infected -> status `quarantined` + `storage.delete` outbox row + quota
release. If clamd is unreachable, retry — never mark clean. Phase 7.
May depend on: core.config, storage_service.
"""
