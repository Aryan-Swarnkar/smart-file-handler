"""Idempotency-Key storage for POST /files/init.

Stores a request hash + the stored response: same key + same body replays the
stored response; same key + different body -> 422. Model lands in Phase 2;
flow in Phase 5.
"""
