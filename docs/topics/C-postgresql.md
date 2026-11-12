# C — PostgreSQL relational modeling (Phase 1)

Code: `app/db/models/*.py`, `app/db/base.py`, migration
`alembic/versions/20261112_bbecbe6aa8ec_initial_schema_with_pgvector.py`.

## What this project demonstrates

**UUID vs bigint keys.** Every PK is a client-generated `uuid.uuid4()`
(`default=uuid.uuid4` on `mapped_column(primary_key=True)`). Why client-side:
the upload flow (P3) must build a `storage_key = "{user_id}/{file_id}"` before
the DB commit — the id must exist in Python first. PG stores them as native
`uuid` (16 bytes, random layout). Trade-off: UUID PKs fragment B-trees more
than monotonic bigints; acceptable at our write rate, and uniqueness across
replicas/import is worth it.

**Composite index column order.** `ix_files_user_created_id (user_id,
created_at, id)` follows the equality-then-range rule: `user_id` is always
filtered with `=` (ownership), `created_at` carries the range/ordering of
keyset pagination, `id` is the tiebreaker — the cursor is
`(created_at, id)`. An ASC composite serves `ORDER BY created_at DESC, id
DESC` via PostgreSQL backward index scan, which is why no DESC expression
index was needed (also keeps autogenerate stable).

**ON DELETE rules.** Every FK declares behavior (verified in
`pg_constraint.confdeltype`: `c` = CASCADE, `n` = SET NULL):

- Ownership chains (users→files/persons/labels/tokens, files→faces/labels):
  CASCADE — deleting a user removes everything derived (privacy requirement).
- `face_instances.person_id → persons.id`: SET NULL — deleting/merging a
  person unassigns faces without destroying detection data.
- `refresh_tokens.replaced_by_id`: SET NULL — chain survives token cleanup.

**Unique constraints as business rules.** `files (user_id, sha256)` =
per-user byte-level dedup; `labels (user_id, name, kind)` — same name may
exist per kind; `refresh_tokens.token_hash` unique — one live token per raw
secret.

**Native enums vs check constraints.** `file_status`, `file_kind`,
`label_kind` are real PG types (`CREATE TYPE`). Pro: catalog-validated, 4
bytes. Con: Alembic autogenerate does NOT diff enum value changes — adding a
value later requires a handwritten `ALTER TYPE ... ADD VALUE`. Documented in
PROGRESS known-gaps.

**Transactions & ACID.** All writes go through one session per request;
Postgres runs migration DDL transactionally (a failed `upgrade` leaves no
half-created tables). Curated operations (merge person, delete file) will be
single-transaction (P7).

**Connection pooling.** `pool_size=10, max_overflow=20, pool_pre_ping=True,
pool_recycle=1800` — pre_ping survives DB restarts, recycle prevents
server-side idle timeouts from killing pooled sockets. PgBouncer notes:
transaction-pooling mode would break `async_sessionmaker` session state
(prepared statements); if we adopt PgBouncer later, set
`statement_cache_size=0` on asyncpg or use PgBouncer 1.21+ protocol features.
