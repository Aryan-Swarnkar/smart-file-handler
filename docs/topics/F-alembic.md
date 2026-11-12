# F — Alembic (Phase 1)

Code: `alembic.ini`, `alembic/env.py` (async), `alembic/script.py.mako`,
`alembic/versions/20261112_bbecbe6aa8ec_initial_schema_with_pgvector.py`.

## How migrations work

A migration is a numbered Python file with `upgrade()`/`downgrade()`
transforming the DB stepwise. The `alembic_version` table stores the current
revision id — that's the whole state machine.

## Async env.py (this repo)

Alembic is sync by default; our engine is asyncpg. `env.py` therefore:
builds an async engine → `await connection.run_sync(do_run_migrations)` where
`do_run_migrations` configures context with the SAME metadata used by models
(`app.db.base.Base`, populated by importing `app.db.models`). URL comes from
`get_settings()` — no URL duplication in the ini file.

## Autogenerate and its blind spots

`alembic revision --autogenerate` diffs `Base.metadata` vs the live DB.
Detected in our initial migration: all 7 tables, every index **including the
HNSW index with `postgresql_using='hnsw'`**, all FK ondelete clauses, unique
constraints, native enum creation.

NOT detected (we hand-fixed in the initial migration — checklist for every
future autogenerate):

1. **Extensions** — `CREATE EXTENSION vector` had to be handwritten before
   any `vector` column exists.
2. **Enum type drops** — `drop_table` leaves native enum types orphaned;
   downgrade needed explicit `DROP TYPE IF EXISTS file_status/file_kind/
   label_kind`.
3. **Missing imports** — generated code referenced `pgvector.sqlalchemy.
   vector.VECTOR` without importing it (crash on first run).
4. **Enum value changes** — never detected at all (adding an enum member =
   handwritten `ALTER TYPE ... ADD VALUE`, non-transactional in PG).
5. **Data migrations** — schema diffs only; data backfills are handwritten.

## Naming conventions make autogenerate stable

`app/db/base.py` `NAMING_CONVENTION` gives every constraint a deterministic
name (`fk_files_user_id_users`, `pk_users`, ...). Without it, PG names
constraints itself and autogenerate emits noisy name-only diffs. One
exception: a 3-column unique constraint got the misleading default name
`uq_labels_user_id` (from `column_0_name`) — fixed with an explicit name
`uq_labels_user_id_name_kind` in both model and migration.

## Migration testing (the checkpoint we ran)

```
python -m alembic upgrade head      # fresh DB: full schema + extension
python -m alembic downgrade base    # drops tables, indexes, enum types
python -m alembic upgrade head      # again — proves re-runnability
```

Plus introspection via psql: `\dx` (vector 0.8.7), `pg_tables`,
`pg_indexes` (HNSW present), `pg_constraint.confdeltype` (CASCADE/SET NULL),
`pg_type` (3 enum types). Both passes clean (see PROGRESS.md).

## Run commands on this machine

`alembic.exe` shim is broken on this Windows host — always use:

```
.venv\Scripts\python.exe -m alembic <command>
```

## Production notes (P9 will wire these)

- Run `alembic upgrade head` as a deploy step BEFORE rolling new api/worker
  replicas (expand-migrate-contract for zero-downtime: additive column first,
  dual-write, backfill, then constrain).
- Adding a NOT NULL column safely: `ADD COLUMN ... NULL` → backfill in
  batches → `ADD CONSTRAINT ... CHECK (NOT NULL) NOT VALID` → `VALIDATE` →
  finally `SET NOT NULL`. Avoids table locks.
