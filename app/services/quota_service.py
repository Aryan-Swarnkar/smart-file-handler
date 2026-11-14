"""Atomic per-user storage quota.

Reservation is a single conditional UPDATE (storage_used_bytes + :n <=
storage_quota_bytes ... RETURNING) — no read-modify-write race. Released on
failure, expiry, and delete. Editors uploading into a shared folder charge
the folder OWNER's quota. Phase 5.
May depend on: repositories.users.
"""
