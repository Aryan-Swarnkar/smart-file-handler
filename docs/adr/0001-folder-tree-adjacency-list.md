# ADR 0001: Folder tree as adjacency list + recursive CTE

- **Status:** accepted (Phase 2)
- **Context:** v3 is Drive-style — folders form an arbitrary tree, and
  authorization (AccessService, Phase 4) must resolve a resource's ancestor
  chain to inherit permissions, while browse (Phase 4) must list children
  cheaply. PostgreSQL offers several tree representations; the choice is
  locked here because the schema ships in the initial migration.

## Decision

`folders` is an **adjacency list**: each row carries `parent_id`
(self-referential FK, `ON DELETE CASCADE`), `NULL` meaning the user's root.

Tree walks use **recursive CTEs with a depth cap of 20**:

```sql
WITH RECURSIVE ancestors AS (
    SELECT id, parent_id, 1 AS depth FROM folders WHERE id = :start
  UNION ALL
    SELECT f.id, f.parent_id, a.depth + 1
    FROM folders f JOIN ancestors a ON f.id = a.parent_id
    WHERE a.depth < 20
)
SELECT ... FROM ancestors;
```

## Why adjacency list over the alternatives

| Option | Verdict | Reason |
|---|---|---|
| **Adjacency list + CTE** (chosen) | ✔ | One obvious write path (single `UPDATE`/`INSERT`); children of a node are an indexed point query; ancestors are one CTE. PG16 executes recursive CTEs efficiently at personal-drive depth (hundreds, not millions, of folders). |
| **ltree** | ✘ | Materialized path per row. Writes move the whole subtree's path strings (every descendant needs a rewrite on rename/move); enforces a *path length* limit rather than a depth limit; ties the schema to a non-core extension for a shape our queries don't need. |
| **Closure table** | ✘ | O(depth) extra rows per folder and a second source of truth to keep transactionally consistent on every move — complexity the access checks don't require. |
| **Materialized path (varchar)** | ✘ | Same subtree-rewrite problem as ltree without even the extension's operators; string-prefix comparisons defeat b-tree index selectivity past the first segment. |

## The depth cap of 20

The CTE stops after 20 levels (`WHERE a.depth < 20`). It is enforced in the
service layer, not by a DB constraint — a CHECK cannot see a cycle longer
than one hop, and triggers were judged out of proportion. The cap:

1. bounds worst-case CTE work to a constant (an attacker cannot craft a
   folder chain that makes ancestor resolution quadratic), and
2. acts as a cycle-breaker: a malformed move can never loop the CTE forever.

20 levels is far beyond any plausible personal-drive nesting; Phase 4's move
logic additionally refuses moves into the mover's own subtree, so legit
operations cannot even approach the cap.

## Risks / future work

- **Benchmark before load test (Phase 12).** If ancestor-walk CTEs show up in
  profiles, the fallback is a write-time `path` materialization (ltree-style)
  maintained only by the folder service — the API surface would not change.
- Move-cycle safety and the exact CTE live with `repositories/folders.py`;
  this ADR records only the representation, not the service logic.

## References

- Schema: `app/db/models/folder.py`, `alembic/versions/20261115_v3_initial.py`
- Spec: PHASE 2 prompt (docs/SPEC.md) — "short docs/adr … saying it will be
  benchmarked later"
