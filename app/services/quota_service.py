"""Atomic per-user storage quota.

Phase 5 shipped this as `repositories/quota.py` (conditional-UPDATE reserve/
release — no read-modify-write race, no RETURNING needed: rowcount decides
the race). Released on failure, expiry (Phase 6 sweeper), and delete.
Editors uploading into a shared folder charge the folder OWNER's quota.
"""
