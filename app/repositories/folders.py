"""Folder tree queries.

Adjacency list + recursive CTE (depth cap 20) for ancestor walks and role
inheritance; move-cycle checks; keyset pagination by name or created_at.
ALL folder SQL lives here. Model lands in Phase 2.
"""
