"""Anonymous public-link routes: GET /s/{token} and descendants (Phase 8).

View-only. Revoked/expired/wrong-password/unknown -> identical 404. No
caching of link validity; Referrer-Policy: no-referrer; strict IP+token
rate limit; downloads are 5-minute presigned GETs.
"""
