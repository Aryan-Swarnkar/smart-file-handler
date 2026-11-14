"""Folder create / rename / list children / move / trash / restore.

Adjacency-list tree with recursive CTE (depth cap 20 — benchmarked later, see
docs/adr/0001); move rejects moving into the owner's own subtree under a
per-owner pg_advisory_xact_lock so concurrent moves cannot create cycles.
Keyset pagination by name or created_at. Every method takes an explicit actor
and calls AccessService first. Phase 4.
May depend on: access_service, repositories.folders.
"""
