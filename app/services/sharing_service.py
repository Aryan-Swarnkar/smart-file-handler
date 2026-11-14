"""Direct permission grants (viewer | editor) + public share links.

Grants: owner-only, share by email (unknown email -> 404; enumeration
trade-off documented in docs/SECURITY.md; rate limited). Links: token =
secrets.token_urlsafe(32), only its sha256 is stored, plaintext shown once;
viewer-only with optional password/expiry/download flag. Anonymous routes are
view-only; revoked/expired/wrong-password/unknown all return an identical
404; link validity is NEVER cached — revocation applies on the next request.
Every create/revoke/grant writes an audit_log row in the same transaction.
Phase 8. May depend on: access_service, repositories.permissions,
repositories.share_links, audit_service.
"""
