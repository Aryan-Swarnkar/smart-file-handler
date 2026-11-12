# E — SQLAlchemy 2.0 async (Phase 1; used everywhere after)

Code: `app/db/session.py`, `app/db/base.py`, all models, health router.

## The stack

`create_async_engine(url, ...)` wraps **asyncpg** — every SQL statement is an
awaited protocol round-trip, zero threadpool hops. `async_sessionmaker(bind=
engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)`.

## Why `expire_on_commit=False`

Default behavior: after `commit()`, every ORM attribute access triggers a
fresh SELECT (expire-then-reload). In async code that reload is a **lazy IO
await** that fires outside an explicit await context → the dreaded
`MissingGreenlet` error. With `expire_on_commit=False`, committed objects
stay populated — we decide when to re-read.

## select() fundamentals (used from P2)

```python
stmt = select(File).where(File.user_id == uid).order_by(
    File.created_at.desc(), File.id).limit(21)
rows = (await session.execute(stmt)).scalars().all()   # ORM objects
```

- `session.execute(stmt)` → `Result`; `.scalars()` unwraps row tuples to
  model instances; `.mappings()` would give dicts.
- `session.get(User, id)` → identity-map PK lookup (may hit cache).
- `await session.flush()` pushes pending INSERT/UPDATEs in the open
  transaction WITHOUT ending it — needed to get DB-generated values
  (`created_at`) or satisfy FK order.
- `await session.commit()` ends the transaction (session stays usable).
- `await session.refresh(obj)` re-SELECTs one object explicitly.

## Loading strategies (N+1 defense)

- Default `lazy="select"` = implicit per-access query = N+1. We avoid
  accessing un-lazy-loaded relationships in request paths.
- `selectinload(File.labels)` — second SELECT with `IN (...)`: right choice
  for to-many lists.
- `joinedload(File.user)` — one LEFT JOIN: right for many-to-one.
- Pick deliberately per query (done in P7 repositories).

## Async session lifecycle (this project)

One session per request via `get_session()` dependency:

```python
async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:   # close guaranteed
        yield session
```

`async with session` scopes the transaction; leaving the block closes the
connection back to the pool. Tests that need isolation build a NullPool
engine per test (`tests/test_db_integration.py`) — the global engine pools
connections that are bound to one event loop, and pytest-asyncio's per-test
loops would otherwise reuse dead-loop sockets (proactor `NoneType.send`).

## pgvector integration

`pgvector.sqlalchemy.Vector(512)` is a first-class type: DDL (`vector(512)`),
parameter binding (asyncpg adapts lists via `pgvector.asyncpg` register —
pgvector handles it), and operators (`embedding.cosine_distance(v)` →
`embedding <=> :v`).

## Repository pattern (enforced from P2)

All `select()/insert()/update()` statements live in `app/repositories/*`;
services orchestrate them; routers never touch sessions. This keeps query
ownership-scoping (`WHERE user_id = :uid`) in exactly one layer, auditable.
