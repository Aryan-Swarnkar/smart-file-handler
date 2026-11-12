# D — Vector search with pgvector (Phase 1; used by Phase 5)

Code: `app/db/models/face.py`, `app/db/models/person.py`, migration
`.../20261112_bbecbe6aa8ec_*.py`, `tests/test_db_integration.py`.

## Embeddings in this system

InsightFace (buffalo_l) turns each detected face into a **512-d float
embedding** where same-identity faces sit close together (cosine). Stored in
`face_instances.embedding vector(512)` (the pgvector type, 4-byte floats) and
`persons.centroid vector(512)` (the running mean of a cluster).

## Distance choice: cosine

`cosine_distance(a,b) = 1 - (a·b)/(|a||b|)` — scale-invariant, standard for
ArcFace embeddings (they are L2-normalized anyway, making cosine ≡ half-L2).
pgvector operator: `<=>`; SQLAlchemy: `FaceInstance.embedding.cosine_distance(v)`.
Verified end-to-end in the roundtrip test: distance to an identical vector is
0.0, and `ORDER BY ... LIMIT 1` returns the correct nearest row.

L2 (`<->`) would punish magnitude differences — wrong for face matching.
Inner product (`<->` in some libs / `<#>` in pgvector) ignores scale entirely.

## HNSW vs IVFFlat — why HNSW

| | HNSW | IVFFlat |
|---|---|---|
| Build | graph, slow-ish | k-means lists, fast |
| Query | O(log n) graph walk | scan `nprobe` lists |
| Recall/speed | high recall at low latency | recall drops unless nprobe↑ |
| Empty table behavior | fine | **needs training data** to build lists |
| Rebuild on drift | no | yes (centroids stale) |

Our clustering inserts faces incrementally per user — IVFFlat's
needs-training + drift problems are disqualifying. HNSW index created with:

```python
Index("ix_face_instances_embedding_hnsw", "embedding",
      postgresql_using="hnsw",
      postgresql_ops={"embedding": "vector_cosine_ops"})
```

Params (defaults at P1): `m=16, ef_construction=64`, query-time
`SET hnsw.ef_search = 40`. Recall≈speed trade-off: raise `ef_search` (P8
EXPLAIN audits will decide) before touching index params.

## Per-user filtering

Every NN query includes `WHERE user_id = :uid` (privacy: **no cross-user
matching ever**). Postgres plans this as index-filtered scan; at small
per-user face counts the HNSW walk stays cheap. If per-user counts explode
(100k+ faces/user), the next step is partitioning by user_id (hash) so each
partition carries its own HNSW graph.

## Centroids & incremental clustering (P5)

Join rule: embedding `d = cosine_distance(embedding, centroid) < THRESHOLD`
(0.6 default, env `FACE_MATCH_THRESHOLD`) → assign to that person and update
centroid = normalized running mean:
`centroid = normalize((centroid * n + e) / (n + 1))`. Else create a new
person. Concurrency safety: per-user `pg_advisory_xact_lock` around
search+insert so two simultaneous files can't fork duplicate persons.
