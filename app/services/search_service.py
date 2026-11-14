"""Search: tsvector full-text + pg_trgm similarity on file name.

Scoped to what the actor can access (owned + directly shared + inside shared
folders via ancestors). Rank by ts_rank; keyset pagination with a stable
tie-break. All SQL lives in repositories. Phase 9.
May depend on: access_service, repositories.files.
"""
