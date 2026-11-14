"""Audit log queries.

Append-only: INSERT only — no update or delete path exists for audit rows
(the audit_service is the sole writer). Model lands in Phase 2; writers in
Phase 8+.
"""
