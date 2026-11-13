# WORKFLOW — smart-file-handler

**One living document. Updated at the end of every phase.**
Describes the exact runtime workflow: what happens when you click/call each
endpoint, which function runs, which file it lives in, what algorithm and
technology it uses, and where each file sits in the chain.

Reading order convention:
`file/function()` — file path relative to repo root.

---

## System map (current: after Phase 2)

```
Browser/curl ──HTTP──▶ API (uvicorn, FastAPI) ──▶ structlog JSON logs
                        │            │
                        │            ├─▶ Redis (readiness ping, auth rate limits)
                        │            ├─▶ PostgreSQL 16+pgvector (SELECT 1 + auth ORM)
                        │            └─▶ SeaweedFS S3 API (unused yet, P3)
                        └─▶ /metrics (prometheus-client)
```

## File responsibility map (current: after Phase 2)

| File | Owns | Used by |
|---|---|---|
| `app/core/config.py` | `Settings` class + `get_settings()`; all env parsing; production secret validation | everything (never imports other app modules) |
| `app/core/logging.py` | `configure_logging()` (structlog JSON/console), `RequestIdMiddleware` (pure ASGI), `get_logger()`, `request_id_var` contextvar | main, errors |
| `app/core/errors.py` | Domain exception classes (+`headers`, e.g. Retry-After) + `install_error_handlers()` → one JSON envelope | main (wires once) |
| `app/api/routers/health.py` | `/healthz`, `/readyz` (redis + db), `/` index routes | main |
| `app/api/routers/auth.py` | `/auth/register|login|refresh|logout|me` — HTTP shapes only, delegates to AuthService | main |
| `app/api/deps.py` | `get_current_user` (Bearer→JWT→User), `rate_limit()` factory, `auth_service` provider, `get_redis` | auth router (all protected routers P3+) |
| `app/schemas/auth.py` | Request/response models (SecretStr, EmailStr, TokenPair) | auth router |
| `app/repositories/users.py` | All `users` table queries | AuthService |
| `app/repositories/refresh_tokens.py` | Token lookup by hash, create, revoke, family nuke | AuthService |
| `app/services/security.py` | argon2 hash/verify + dummy-verify, JWT create/decode, refresh token material (SHA-256 only stored) | AuthService, deps |
| `app/services/auth.py` | register/login/refresh(rotation+reuse)/logout business rules; one commit per operation | deps |
| `app/services/rate_limit.py` | Redis fixed-window limiter (INCR+EXPIRE), fail-open policy | deps |
| `app/db/base.py` | `Base(DeclarativeBase)` + constraint naming convention + `CreatedAtMixin` | all models |
| `app/db/session.py` | `engine` (create_async_engine), `session_factory`, `get_session` dependency | every router |
| `app/db/models/*.py` | 7 ORM models (User, RefreshToken, File, Person, FaceInstance, Label, FileLabel) | Alembic metadata, repositories |
| `alembic/env.py` | Async migration runner (asyncpg via run_sync) | `python -m alembic ...` |
| `alembic/versions/20261112_bbecbe6aa8ec_*.py` | Initial schema (tables, vector extension, HNSW index, enums) | migration history |
| `app/main.py` | `create_app()` factory + module `app`; lifespan (redis pool, engine dispose), middleware order, router registration, `/metrics` route | uvicorn, tests |
| `tests/conftest.py` | env bootstrapping before app import, `client` fixture (lifespan-aware), real compose Redis + per-run rate-limit namespace | all tests |
| `tests/test_smoke.py` | P0 endpoint contract tests (incl. dead-redis readiness) | pytest |
| `tests/test_db_integration.py` | pgvector roundtrip + cosine NN + enum persistence (rollback-isolated) | pytest |
| `tests/test_auth.py` | 18 auth integration tests (register/login/refresh/logout/429) | pytest |
| `Dockerfile` | multi-stage build: `builder` → `api` / `worker` targets | compose, CI |
| `docker-compose.yml` | postgres(pgvector), redis, seaweedfs(S3), api | local dev |
| `deploy/seaweedfs/s3.json` | SeaweedFS S3 credentials (dev creds `minioadmin`) | s3 container |

## Workflow 1 — App startup (what happens before the first request)

1. `docker compose up` builds `api` target (see Workflow 6) and starts
   postgres, redis, seaweedfs; waits for all three `healthcheck`s.
2. Container CMD `uvicorn app.main:app` imports `app/main.py` → calls
   `create_app()`:
   - `configure_logging(level, format)` in `app/core/logging.py`:
     - builds structlog processors:
       `merge_contextvars → add_log_level → TimeStamper(ISO, UTC) →
       dict_tracebacks → _add_request_id (injects request_id contextvar) →
       JSONRenderer`
     - installs ONE `ProcessorFormatter` on the root logger + reroutes
       `uvicorn`/`uvicorn.access`/`uvicorn.error` loggers through it
       (propagate=False) so every stdout line is uniform JSON.
   - `lifespan` opens `redis.asyncio.Redis.from_url(REDIS_URL,
     decode_responses=True)` and stores it on `app.state.redis` (a connection
     pool — the only shared in-memory object; user state is never stored,
     keeping the API stateless for N replicas).
   - middleware registered: `RequestIdMiddleware` (innermost) → CORS (if
     origins configured).
   - `install_error_handlers(app)` from `app/core/errors.py` registers 4
     handlers: `AppError`, Starlette `HTTPException`, `RequestValidationError`,
     bare `Exception` — all emit
     `{"error": {"code", "message", "details"?}, "request_id"}`.
   - `include_router(health.router)`; `@app.get("/metrics")` route returns
     `generate_latest()` with `CONTENT_TYPE_LATEST` (prometheus exposition
     format). Deliberately NOT a mounted ASGI sub-app: a mount 307-redirects
     `/metrics` → `/metrics/`, which breaks plain curl and naive scrapers.
3. uvicorn serves on `0.0.0.0:8000`.

**Loop algorithm note:** uvicorn runs a single asyncio event loop per process;
every route is a coroutine. No thread pools are used in the API process at P0.

## Workflow 2 — `GET /healthz` (liveness)

1. Request enters `RequestIdMiddleware.__call__()` (`app/core/logging.py`):
   - reads `x-request-id` header; accepts it only if 1–64 chars (never trusts
     content), else `uuid.uuid4().hex`
   - `request_id_var.set(id)` + `structlog.contextvars.bind_contextvars`
   - wraps `send` so `x-request-id` is appended to the response.
2. Route `healthz()` in `app/api/routers/health.py` returns
   `{"status": "ok"}` — **no dependency touch** by design: if the process is
   alive it answers, so a DB outage cannot fail liveness and trigger
   restart-loops.
3. Response: `200 {"status":"ok"}` + `x-request-id` header; log line contains
   `request_id`.

## Workflow 3 — `GET /readyz` (readiness)

1. Same middleware path as Workflow 2.
2. `readyz()` (`app/api/routers/health.py`):
   - fetches `request.app.state.redis`
   - `await redis.ping()` (async Redis protocol RESP PING)
   - on `RedisError`/`OSError` → check marked `down`
3. All up → `200 {"status":"ready","checks":{"redis":"up"}}`;
   any down → `503 {"status":"unavailable","checks":{...}}`.
4. Scope: redis + postgres (P1). Storage (SeaweedFS) check lands in P3.

## Workflow 4 — Any error response (envelope)

1. Service code raises e.g. `NotFoundError` (`app/core/errors.py`).
2. Exception bubbles to the registered handler → `JSONResponse` with
   `error.code = "not_found"`, HTTP 404.
3. Unhandled `Exception` → handler logs full traceback via
   `logger.exception()` (structlog) but returns generic
   `{"error":{"code":"internal_error","message":"Internal server error."}}` —
   internals never leak to clients.

## Workflow 5 — Data model (Phase 1)

**Entity relationships** (all FKs declare explicit ON DELETE):

```
users 1─┬─* refresh_tokens          (CASCADE; replaced_by self-FK SET NULL)
        ├─* files                   (CASCADE)
        ├─* persons                 (CASCADE)
        ├─* labels                  (CASCADE)
        │      files *─* labels  via file_labels (CASCADE both sides)
        └─(files) *───* face_instances          (file_id CASCADE, user_id CASCADE)
persons 1──* face_instances          (person_id SET NULL: deleting a person
                                      unassigns faces, never deletes them)
```

**Table-by-table** (`app/db/models/`):

- `users`: uuid PK (client-side `uuid.uuid4`), lowercased-unique email,
  argon2 hash (stored P2), `created_at` (DB `now()`).
- `refresh_tokens`: token_hash (SHA-256, unique), expires_at, revoked_at,
  replaced_by_id → rotation chain for reuse detection.
- `files`: status enum `file_status(uploaded,processing,done,failed)`, kind
  enum `file_kind(image,pdf,document,text,other)`, sha256 + storage_key,
  BigInteger size (int4 overflows at 2GiB), doc_topic denormalized for cheap
  filters, error text for failures. Indexes: unique `(user_id, sha256)` dedup
  + `ix_files_user_created_id(user_id, created_at, id)` for keyset pagination.
- `persons`: centroid `vector(512)` NOT NULL, face_count, cover_face_id
  (plain uuid — deliberate non-FK, avoids cycle with face_instances; delete
  flows recompute it).
- `face_instances`: embedding `vector(512)`, bbox `double precision[]`
  (x1,y1,x2,y2), det_score. Indexes: HNSW
  `ix_face_instances_embedding_hnsw` (`vector_cosine_ops`) for ANN search +
  `ix_face_instances_user_person(user_id, person_id)` for ownership-scoped
  assignment queries.
- `labels`: unique `(user_id, name, kind)` — "work" can exist as thing AND
  doc_topic for the same user; kind enum `label_kind(thing, doc_topic)`.
- `file_labels`: PK `(file_id, label_id)` + `ix_file_labels_label_id` for the
  "files by label" reverse scan.

**Session lifecycle** (`app/db/session.py`): `create_async_engine(asyncpg,
pool_size=10, pool_pre_ping, pool_recycle=1800)` → `async_sessionmaker(
expire_on_commit=False, autoflush=False)`. One session per request:
`get_session()` yields inside `async with` (guaranteed close). Idle-in-
transaction prevention comes from the `async with` block scoping.

**Migration workflow** (run via `.venv\Scripts\python.exe -m alembic ...` —
the `alembic.exe` shim is broken on this host):

1. `alembic revision --autogenerate -m "..."` diffs `Base.metadata` (all
   models registered by importing `app.db.models`) against the live DB.
2. Hand-fix blind spots (extensions, enum type drops, constraint naming).
3. `alembic upgrade head` applies inside one transaction (Postgres
   transactional DDL — partial failure rolls back completely).
4. `alembic downgrade base` must return the DB to truly clean: tables →
   explicit `DROP TYPE` for the 3 native enums.

**Vector math** (used P5, schema ready now): cosine distance operator `<=>`
exposed by pgvector as `FaceInstance.embedding.cosine_distance(vec)`;
HNSW gives approximate-NN with `vector_cosine_ops` — recall trade-off tuned
via `hnsw.ef_search` at query time (default 40).

## Workflow 6 — Auth (Phase 2)

**Click → code path** (register):
1. `POST /auth/register` → `app/api/routers/auth.py:register` — schema
   `RegisterIn` validates email format (EmailStr via email-validator, DNS
   sanity off) + password length (SecretStr, 8–128).
2. Rate-limit dependency `rate_limit("auth:register", 5/min)` fires FIRST
   (`app/services/rate_limit.py`): Redis `INCR rl:{ns}:auth:register:{ip}`,
   `EXPIRE 60` on first hit; count > limit → `RateLimitedError` 429 with
   `Retry-After` = TTL. Redis down → fail-open (log + allow).
3. `AuthService.register` (`app/services/auth.py`): email lowercased →
   `UserRepository.get_by_email` (dup → 409 `email_taken`) → argon2id hash
   (`app/services/security.py`, PasswordHasher defaults: 64 MiB, t=3, p=4)
   → insert → commit. Concurrent-register race loses to the unique index →
   IntegrityError caught → same 409.
4. Response: `UserOut` (id, email, created_at). password NEVER returns.

**Login flow**: same rate limit (10/min) → lookup; user missing →
`dummy_verify()` burns argon2 cost so timing matches a real verify, then 401
`invalid_credentials` (identical message for wrong password — enumeration
cannot distinguish). Success → opaque 256-bit refresh token
(`secrets.token_urlsafe(32)`), only SHA-256 stored; HS256 access JWT
(sub=user uuid, iat, exp=+15min, typ=access) → `TokenPair`.

**Refresh rotation + reuse detection** (the security core):
```
client ──refresh(A)──▶ lookup sha256(A)
                       ├─ missing ──────────────▶ 401
                       ├─ revoked_at IS NOT NULL ─▶ REUSE! UPDATE all live
                       │                            tokens of user → revoked;
                       │                            log warning; 401
                       ├─ expired ─▶ revoke; 401
                       └─ live ─▶ issue NEW token B, revoke A with
                                  replaced_by_id=B.id (audit chain), commit,
                                  return pair(B)
```
Each token is single-use; a replayed one is evidence of theft → the whole
family dies and every device must re-login. Presented tokens are `.strip()`ed
first (P2.1: whitespace in copy-pasted tokens used to fail silently).
Verified live in P2 pass 2.

**Logout** (fixed after user-reported bug, P2.1): idempotent per RFC 7009 —
presents refresh token → `.strip()` it (copy-paste whitespace used to change
the SHA-256 and fail a "correct" token) → lookup → live? revoke + commit;
unknown/already-revoked? silent no-op → **always 204, no 401 possible**.
Why uniform-success beats the old strict-401: (a) a client can never "fail
to log out" — e.g. after refresh rotation or a reuse-detection family nuke
made its held token stale (exactly the user's bug: refresh then logout →
401); (b) a uniform response removes the "does this token hash exist?"
probing oracle. Access token keeps working ≤15 min (stateless JWT
trade-off; no blacklist — short expiry is the bound).

**429 rate-limit body (P2.1)**: humans get the timing in the message, not
just the header — `{"error": {"code": "rate_limited", "message": "Too many
login attempts. You can try again in 28 seconds.", "details":
{"retry_after_seconds": 28}}, "request_id": ...}` + `Retry-After: 28`
header (RFC-mandated, machines/CDNs read it). `format_retry_after()`
(app/api/deps.py): seconds < 60 → "N seconds", above → "M minutes".

**`/auth/me`** → `get_current_user` (`app/api/deps.py`): `HTTPBearer`
(auto_error=False so a missing header raises OUR envelope, not FastAPI's
403) → `decode_access_token` (PyJWT, typ must be "access") → `session.get`
User → missing user → 401 (never 404: no account probing).

**Request flow layering** (enforced from P2):
`router (HTTP shapes) → service (business rules, owns commit) → repository
(owns all SQL) → model`. A router never touches a session; a repository
never commits; a service never builds SQL.

**Technologies**: argon2-cffi (argon2id), PyJWT (HS256), redis-py asyncio
(INCR/EXPIRE/TTL), email-validator, pydantic SecretStr.

## Workflow 7 — Test run (`uv run pytest`)

1. `tests/conftest.py` executes FIRST: sets `ENV=test`, `SECRET_KEY`,
   `DATABASE_URL`, `REDIS_URL` (closed port 127.0.0.1:1) via
   `os.environ.setdefault` **before importing app** (module-level `app =
   create_app()` reads settings at import time).
2. `client` fixture: `create_app()` →
   `async with app.router.lifespan_context(app)` (httpx `ASGITransport` does
   NOT run lifespan, so we run it explicitly — startup/shutdown really
   execute) → `httpx.AsyncClient(transport=ASGITransport(app), ...)`.
3. `_reset_settings_cache` (autouse): clears the `get_settings` lru_cache
   around every test so env changes take effect.
4. `tests/test_smoke.py`: 6 tests — healthz shape, index links, OpenAPI
   contains `/healthz`, 404 envelope carries request_id, readyz is 503 and
   names redis as `down` (redis client exists but ping fails), and
   client-supplied request-id is echoed.

## Workflow 8 — Docker image build (Dockerfile)

1. `base` (`python:3.12-slim`): sets `PYTHONDONTWRITEBYTECODE`,
   `PYTHONUNBUFFERED`, `WORKDIR /app`.
2. `builder`: copies pinned `uv` binary (`ghcr.io/astral-sh/uv:0.11.26` —
   same version as host so `uv.lock` format matches), copies
   `pyproject.toml`+`uv.lock`, runs `uv sync --frozen --no-dev
   --no-install-project` (frozen = fail if lock out of sync; layer cached).
3. `runtime-common`: copies `.venv` from builder, prepends it to `PATH`,
   copies `app/` source, creates non-root `appuser`.
4. `api` target: CMD `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
   `worker` target: same layers, CMD `arq app.workers.settings.WorkerSettings`
   (inert until Phase 4 adds `app/workers`).

## Workflow 9 — `docker compose up` (local stack)

| Service | Image | Port | Notes |
|---|---|---|---|
| postgres | `pgvector/pgvector:pg16` | **5433**→5432 | pgvector build of PG16 (needed P1); host 5432 occupied by stale stage-1 container `beta-db-1` |
| redis | `redis:7-alpine` | 6379 | AOF persistence on |
| s3 | `chrislusf/seaweedfs:3.99` | 9000→8333, 9333 | S3 API + master UI; creds from `deploy/seaweedfs/s3.json` |
| api | built (`target: api`) | 8000 | env overrides `.env` (env vars beat dotenv in pydantic-settings) |

`depends_on` uses `service_healthy` conditions, not just "started".

**Substitution note:** MinIO community edition was discontinued mid-2025+
(archived repo, images removed from Docker Hub/quay, binaries 410 Gone).
SeaweedFS is the user-approved local S3 backend; production uses S3/R2 via
aioboto3 — identical code path (S3 API), only the dev container differs.

## Technologies in play (through Phase 2)

- **FastAPI** (ASGI) + **uvicorn** — HTTP layer, fully async handlers.
- **pydantic-settings v2** — `Settings`; env vars > `.env` > code defaults;
  `@model_validator(mode="after")` INSIDE the class enforces production
  secret strength (this fixes stage-1's broken module-level validator).
- **structlog** — JSON structured logs, contextvars-based request IDs.
- **prometheus-client** — `/metrics` (default process metrics at P0).
- **redis-py asyncio** — readiness ping (pool reused for rate limiting P2,
  ARQ broker P4).
- **SQLAlchemy 2.0 async** — `create_async_engine(asyncpg)`,
  `async_sessionmaker(expire_on_commit=False)`, `Mapped/mapped_column`
  declarative models, one session per request via dependency.
- **pgvector 0.8.7** — `Vector(512)` columns, cosine-distance operator,
  HNSW ANN index.
- **Alembic (async env.py)** — versioned schema; transactional DDL.
- **argon2-cffi** — argon2id password hashing (memory-hard, GPU-unfriendly).
- **PyJWT** — HS256 access tokens (sub/iat/exp/typ claims).
- **email-validator** — pydantic `EmailStr` syntax checking.
- **httpx + pytest-asyncio** — in-process ASGI integration tests.
- **uv** — locked, cached dependency installs (host + Docker builder).
- **Docker Compose** — 4-service local stack with health-gated startup.
