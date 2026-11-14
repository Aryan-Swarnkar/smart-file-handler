"""Outbox queries.

Insert outbox rows in the SAME transaction as the state change they announce;
the relay claims unpublished rows with SELECT ... FOR UPDATE SKIP LOCKED in
small batches and marks them published (at-least-once delivery). Partial
index on unpublished rows. Model lands in Phase 2; relay in Phase 6.
"""
