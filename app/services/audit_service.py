"""Append-only audit_log writer.

Rows: login, share/link create/revoke, delete/purge, restore. Append-only by
construction — no update or delete code path exists anywhere for audit rows.
Phase 8+.
May depend on: repositories.audit_log.
"""
