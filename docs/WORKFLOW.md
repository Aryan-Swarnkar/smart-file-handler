# WORKFLOW — smart-file-handler

**One living document. Updated at the end of every phase.**
Describes the exact runtime workflow: what happens when you click/call each
endpoint, which function runs, which file it lives in, what algorithm and
technology it uses, and where each file sits in the chain.

Reading order convention:
`file/function()` — file path relative to repo root.

---

## System map (current: after Phase 4)

```
web/ React UI (Vite :5173) ──▶ API (uvicorn, FastAPI) ──▶ structlog JSON logs
        │  presigned PUT bytes ──▶ SeaweedFS :9000 (direct, CORS-allowed)     ▲
        │                              │            │
        └──HTTP──▶ API                 │            ├─▶ Redis (readiness ping, auth rate limits)
                       │               │            ├─▶ PostgreSQL 16+pgvector (SELECT 1 + auth/files ORM)
                       │               │            ├─▶ SeaweedFS S3 API (presigned PUT/GET, head, delete)
                       │               │            │      ▲ bytes go client→S3 DIRECTLY (presigned URLs)
                       │               │            └─▶ /metrics (prometheus-client)
                       │               └─▶ /metrics
                       └─▶ /readyz polled every 15s by web/src/components/StatusStrip.tsx
```

## File responsibility map (current: after Phase 4)

| File | Owns | Used by |
|---|---|---|
| `app/core/config.py` | `Settings` class + `get_settings()`; all env parsing; production secret validation | everything (never imports other app modules) |
| `app/core/logging.py` | `configure_logging()` (structlog JSON/console), `RequestIdMiddleware` (pure ASGI), `get_logger()`, `request_id_var` contextvar | main, errors |
| `app/core/errors.py` | Domain exception classes (+`headers`, e.g. Retry-After) + `install_error_handlers()` → one JSON envelope | main (wires once) |
| `app/api/routers/health.py` | `/healthz`, `/readyz` (redis + db + **s3 HeadBucket**), `/` index routes | main |
| `app/api/routers/auth.py` | `/auth/register|login|refresh|logout|me` — HTTP shapes only, delegates to AuthService | main |
| `app/api/routers/files.py` | `/files` CRUD: init 201 / complete / list+filters / get / url / PATCH rename / DELETE 204 — HTTP shapes only | main |
| `app/api/deps.py` | `get_current_user`, `rate_limit()` factory, `auth_service`, **`get_storage` + `files_service`** providers | auth + files routers |
| `app/schemas/auth.py` | Request/response models (SecretStr, EmailStr, TokenPair) | auth router |
| `app/schemas/files.py` | `FileInitIn/Out`, `FileOut`, `FileCompleteOut(+deduplicated)`, `FileRenameIn`, `FileUrlOut`, `FilePageOut` | files router |
| `app/repositories/users.py` | All `users` table queries | AuthService |
| `app/repositories/refresh_tokens.py` | Token lookup by hash, create, revoke, family nuke | AuthService |
| `app/repositories/files.py` | All `files` queries + **keyset cursor encode/decode** + `Page` + **P4 sweep queries** (enqueue candidates = uploaded + sha256 set; delete orphan inits) | FileService, worker jobs |
| `app/services/security.py` | argon2 hash/verify + dummy-verify, JWT create/decode, refresh token material (SHA-256 only stored) | AuthService, deps |
| `app/services/auth.py` | register/login/refresh(rotation+reuse)/logout business rules; one commit per operation | deps |
| `app/services/files.py` | **init/complete/list/get/url/rename/delete business rules**: size limit, HEAD→sniff→sha256→dedup→commit, S3-before-DB delete ordering, **P4: best-effort ARQ enqueue after a processable commit** | deps |
| `app/workers/pipeline.py` | **P4 job coroutines (the worker's service layer)**: `process_file` (claim uploaded→processing→pipeline seam→done; idempotent claim guard; Retry with exponential backoff; terminal failure → status=failed + error), `sweep_uploads` (enqueue unclaimed completed rows + delete stale orphan inits) | WorkerSettings |
| `app/workers/settings.py` | ARQ `WorkerSettings` (`arq app.workers.settings.WorkerSettings`): functions + cron sweep (quantized to the minute grid), startup/shutdown mirroring the API lifespan (session factory + StorageService into ctx), `max_tries`, redis settings from `REDIS_URL` | worker Docker target |
| `app/services/sniffing.py` | **Pure byte classifier**: magic JPEG/PNG/WEBP/PDF/ZIP, office-ext disambiguation, UTF-8 text probe; filename sanitizers | FileService |
| `app/services/storage.py` | **The only S3 client**: aioboto3 SigV4, presign put/get (public endpoint), head/read/delete, bucket + CORS bootstrap | FileService, readyz |
| `app/services/rate_limit.py` | Redis fixed-window limiter (INCR+EXPIRE), fail-open policy | deps |
| `app/db/base.py` | `Base(DeclarativeBase)` + constraint naming convention + `CreatedAtMixin` | all models |
| `app/db/session.py` | `engine` (create_async_engine), `session_factory`, `get_session` dependency | every router |
| `app/db/models/*.py` | 7 ORM models (User, RefreshToken, **File (nullable sha256 + uq dedup)**, Person, FaceInstance, Label, FileLabel) | Alembic metadata, repositories |
| `alembic/env.py` | Async migration runner (asyncpg via run_sync) | `python -m alembic ...` |
| `alembic/versions/20261112_bbecbe6aa8ec_*.py` | Initial schema (tables, vector extension, HNSW index, enums) | migration history |
| `alembic/versions/20261113_a3f1c2d4e5f6_*.py` | P3: `files.sha256` nullable + `uq_files_user_id_sha256` (NULLS DISTINCT) | migration history |
| `app/main.py` | `create_app()` factory + module `app`; lifespan (redis pool, **StorageService start/close**), middleware order, router registration, `/metrics` route | uvicorn, tests |
| `tests/conftest.py` | env bootstrapping before app import, `client` fixture (lifespan-aware), real compose Redis + per-run rate-limit namespace, **autouse rate-limit flush per test** | all tests |
| `tests/test_smoke.py` | P0 endpoint contract tests (incl. dead-redis readiness) | pytest |
| `tests/test_db_integration.py` | pgvector roundtrip + cosine NN + enum persistence (rollback-isolated) | pytest |
| `tests/test_auth.py` | 18 auth integration tests (register/login/refresh/logout/429) | pytest |
| `tests/test_files.py` | 19 files integration tests (real SeaweedFS PUT/GET: kinds, sniff-wins, dedup, 409/422, pagination, 404 matrix, rename/delete, **presign public endpoint**) | pytest |
| `tests/test_worker.py` | 9 worker tests (real DB + Redis db 15): claim/done, skip-when-done, missing/incomplete guards, Retry backoff + terminal failed, sweep enqueue/idempotency/orphan-init deletion, API complete→queue | pytest |
| `scripts/p3_live_check.ps1` | 29-check live walkthrough of every files route + readyz + CORS preflight | manual verification |
| `scripts/p4_live_check.ps1` | 10-check live walkthrough of the queue: complete→worker-processed=done, Matrix A no-queue, dedup, filters, cleanup | manual verification |
| `web/src/types.ts` | TS mirrors of `app/schemas/{auth,files}.py` + `ApiErrorBody` envelope + `ReadyBody`; `fmtBytes`/`fmtDate` helpers | every web module (kept in sync manually) |
| `web/src/api/session.ts` | Token store on localStorage (`sfh.access`/`sfh.refresh`) + tiny listener registry so the fetch client can wipe a dead session | client, AuthContext |
| `web/src/api/client.ts` | Fetch wrapper: `API_URL` (from `VITE_API_URL`), bearer header, envelope→`ApiError`, **single-flight 401→refresh→replay** (`_retried` bound, refresh rotates single-use tokens so parallel 401s share ONE refresh) | all web api modules |
| `web/src/api/auth.ts` | `/auth/*` calls; logout sends the refresh token in the BODY (no Bearer), always-204 | AuthContext |
| `web/src/api/files.ts` | `/files/*` calls: init/complete/list (cursor passed verbatim)/url/PATCH/DELETE | UploadQueue, FileGrid |
| `web/src/api/upload.ts` | **XHR PUT** to the presigned URL (XHR not fetch: only browser primitive with upload progress events + abort); no auth header — SigV4 signature IS the authorization | UploadQueue |
| `web/src/state/AuthContext.tsx` | Hydrates session on load (`me()`), login/register/logout, reacts to external `session.clear()` (dead-refresh path) | App |
| `web/src/components/StatusStrip.tsx` | Polls PUBLIC `/readyz` every 15s → 3 dots (redis/db/s3) + "all up"/"degraded"/"API unreachable" | Shell topbar |
| `web/src/components/AuthScreens.tsx` | Login/Register tabs; envelope errors shown verbatim (429 shows countdown via Toaster) | App |
| `web/src/components/Dropzone.tsx` | Drag & drop files OR folders (entries grabbed synchronously, then walked recursively; `readEntries` batch loop) + file/folder pickers; synthetic-drop fallback to `dataTransfer.files` | Shell |
| `web/src/components/UploadQueue.tsx` | `useUploadQueue` hook (state in ref-mirror so async continuations see the latest list) + `UploadQueueView`: per-file init→PUT(progress 0–90%)→complete→done|dedup|failed|canceled, concurrency 3, cancel via AbortController, retry-failed-only, 25 MB client-side pre-check (server re-verifies) | Shell |
| `web/src/components/FileGrid.tsx` | Cards over real keyset pagination (cursor + "Load more"), kind/status/topic filters with **stale-response seq guard** (rapid filter changes: only the latest fetch may touch state), view/download (presigned URLs), rename (double-click inline), delete (confirm dialog) | Shell |
| `web/src/components/Toaster.tsx` | Event-based toasts (`sfh-toast` CustomEvent, no prop drilling); 429 renders a live countdown from `details.retry_after_seconds` | App root |
| `web/src/App.tsx` / `main.tsx` / `styles.css` | Shell composition (queue hoisted via `useUploadQueue`), React 18 mount, dark theme (no CSS framework) | — |
| `web/vite.config.ts` / `tsconfig.json` / `package.json` | Vite dev server on 5173 (the origin whitelisted by API CORS + bucket CORS), TS strict (`noUnusedLocals` etc.), React 18 + Vite 5 + TS 5.6 | `npm run dev` / `npm run build` |
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
   - db: `SELECT 1` through a fresh `session_factory()` session
   - s3: `await storage.ping()` — HeadBucket via the lifespan-started
     `StorageService` on `app.state.storage` ("not_configured" if the
     lifespan never ran); on any exception → check marked `down`
3. All up → `200 {"status":"ready","checks":{"redis":"up","db":"up","s3":"up"}}`;
   any down → `503 {"status":"unavailable","checks":{...}}`.
4. Scope: redis (P0) + postgres (P1) + **s3 (P3, `storage.ping()` = HeadBucket
   via the api's own S3 client — proves endpoint, creds, and bucket)**.

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

## Workflow 7 — File upload (Phase 3)

**Click → code path** (the presigned dance — bytes NEVER pass through the API):

```
client                        API                           SeaweedFS
  │ POST /files/init            │                               │
  │────────────────────────────▶ row created (sha256=NULL)      │
  │◀─── 201 {file_id, upload_url, expires_in} ──────────────────│
  │ PUT upload_url (raw bytes) ─────────────────────────────────▶ object stored
  │ POST /files/{id}/complete    │                               │
  │────────────────────────────▶ HEAD ──────────────────────────▶│
  │                              │ stream 1MiB chunks: sha256 +  │
  │                              │ sniff head 8KiB               │
  │                              │ dedup lookup (user_id,sha256) │
  │◀─── 200 FileCompleteOut ─────┤ (409/422 on failures)         │
```

1. **`POST /files/init`** (`app/api/routers/files.py:init_upload` →
   `FileService.init_upload` in `app/services/files.py`):
   - validate `size ≤ MAX_UPLOAD_MB` (env-driven; over → 422
     `file_too_large` with `details.max_bytes`);
   - `sanitize_filename`/`sanitize_extension` (`app/services/sniffing.py`):
     basename only, 255-char DB cap, lowercase alnum extension ≤10;
   - storage key `{user_id}/{file_id}.{ext}` (uuid v4; extension from the
     name — **advisory only**, sniff re-decides at complete);
   - row created NOW with `sha256=NULL` (NULLs never collide under the
     `uq_files_user_id_sha256` unique constraint → pending uploads of the
     same user can't dedupe-collide), `status=uploaded`, `kind=NULL`;
   - `StorageService.presign_put(key, size)` — SigV4 presigned PUT URL bound
     to the **public endpoint** (`S3_PUBLIC_ENDPOINT_URL`): signatures embed
     the host the CLIENT connects to, and containers sign internally with
     `s3:8333` while clients reach `localhost:9000` — hence the separate
     presign-only client (`app/services/storage.py`);
   - commit → `201 {file_id, upload_url, expires_in=PRESIGNED_URL_EXPIRE_SECONDS}`.
2. **Client `PUT`s the bytes straight to SeaweedFS** with the presigned URL.
   No auth header — the URL's signature IS the authorization (SigV4, 15-min
   expiry). Client `Content-Type` is irrelevant (v4 presigned URLs sign only
   the host header).
3. **`POST /files/{id}/complete`** → `FileService.complete_upload`:
   - ownership first: row missing or foreign user → 404 (never 403);
   - **idempotent**: `sha256` already set → return the same result again
     (at-least-once client retries are safe);
   - `storage.head(key)` → object missing → 409 `upload_not_found` (client
     never PUT or URL expired); `ContentLength != declared size` → 409
     `size_mismatch` (the presigned PUT is Content-Length-bound: a wrong-
     length PUT is rejected 403 upstream — the HEAD check is the defensive
     backstop for objects that reach the key by other means);
   - `read_range(key, 8192)` (`SNIFF_SAMPLE_BYTES`) → `sniff()` pure
     classifier (`app/services/sniffing.py`), matrix locked with the user:
     magic bytes decide; a fake `.jpg` (MZ/exe bytes) → `kind=other` —
     **sniff wins over the filename**;
   - `_hash_object` — streaming SHA-256 over 1 MiB chunks (constant memory,
     25 MB cap keeps this one pass ~cheap);
   - **dedup**: `get_by_user_and_hash(user_id, sha256)` hit (different row)
     → delete the just-uploaded S3 object + delete the duplicate row +
     commit → return the ORIGINAL row with `deduplicated: true`
     (Google-Photos semantics, user decision #2);
   - else: persist `sha256/kind/mime_type/size`; `kind=other` →
     `doc_topic="other"`, `status=done` immediately (nothing for a worker
     to do); processable kinds stay `status=uploaded` until P4 enqueues.
4. **`GET /files`** → `FileService.list_files` → `FileRepository.list_page`:
   - **keyset pagination only** (user-locked: no OFFSET ever). Cursor =
     base64url of `{created_at.isoformat()}|{id}`; SQL predicate is a
     PostgreSQL **row-value tuple comparison** `(created_at,id) < (c1,c2)`
     which walks the composite index `ix_files_user_created_id` (ASC index
     serves DESC via backward scan) — O(log n) per page at any depth;
   - `limit` 1–100 (default 20), `+1` fetch probes `has_more` without a
     second query; tampered cursor → 422 `bad_cursor`;
   - filters: `kind`, `doc_topic`, `status` (alias param `status` — reserved
     name); invalid enum → 422 `bad_filter`.
5. **`GET /files/{id}/url`** → `presign_get(key, download)`; `download=true`
   adds `ResponseContentDisposition: attachment` (forces save-as). URL is
   opaque + expiring — no public bucket ACLs, ever.
6. **`PATCH /files/{id}`** → rename via `sanitize_filename` (display name
   only — the S3 key never changes).
7. **`DELETE /files/{id}`** → **hard delete** (user decision #3): S3 object
   deleted FIRST, then the row. Ordering rationale: S3-delete failure leaves
   the row intact (client retries; S3 delete is idempotent), while a
   committed row pointing at an already-deleted object would be a permanent
   dangling reference. `DELETE /files/{id}` for a foreign id → 404.
8. **Ownership invariant everywhere** (`FileService.get_file`): any
   foreign/cross-user id → `NotFoundError` 404 — indistinguishable from a
   nonexistent id (no probing oracle; user decision pattern carried from P2).

**Startup bootstrap** (`StorageService.start()` from `app/main.py` lifespan):
build aioboto3 client (`AioConfig(signature_version="s3v4")`) against
`S3_ENDPOINT_URL`; if `S3_PUBLIC_ENDPOINT_URL` differs, build a second
presign-only client against it; `ensure_bucket()` = HeadBucket → CreateBucket
(races between 2 replicas handled) → **`_ensure_cors()`**: PutBucketCors with
the API's `CORS_ORIGINS` list (SeaweedFS otherwise answers browser OPTIONS
preflights with 403 AccessDenied; curl never triggers preflight, browsers
always do — verified live). Shutdown: close both clients.

**Technologies**: aioboto3 (async boto3) + botocore SigV4 presigned URLs,
SeaweedFS S3 API (dev) / AWS S3 / Cloudflare R2 (prod), hashlib streaming
SHA-256, PostgreSQL row-value comparison + composite index, base64url opaque
cursors.

## Workflow 8 — Test run (`uv run pytest`)

1. `tests/conftest.py` executes FIRST: sets `ENV=test`, `SECRET_KEY`,
   `DATABASE_URL` (compose postgres on **5433** — host 5432 is occupied),
   `REDIS_URL` (compose redis — REAL, auth rate limiting needs it), and a
   unique per-run `RATELIMIT_NAMESPACE` via `os.environ.setdefault` **before
   importing app** (module-level `app = create_app()` reads settings at
   import time).
2. `client` fixture: `create_app()` →
   `async with app.router.lifespan_context(app)` (httpx `ASGITransport` does
   NOT run lifespan, so we run it explicitly — startup/shutdown really
   execute, including StorageService + bucket/CORS bootstrap) →
   `httpx.AsyncClient(transport=ASGITransport(app), ...)`.
3. `_reset_settings_cache` (autouse): clears the `get_settings` lru_cache
   around every test so env changes take effect.
4. `_flush_rate_limits` (autouse, moved to conftest in P3): deletes
   `{namespace}:auth:*` keys before EVERY test — all API tests share client
   IP 127.0.0.1, and the files suite registers ~18 users per run, which
   would otherwise 429 the rest of the suite.
5. **ASGITransport gotcha (P3)**: the test client routes EVERY request —
   even absolute URLs — into the ASGI app itself, so an S3 PUT "via" the
   test client never leaves the process (it returned our own 404 envelope).
   `tests/test_files.py` therefore does presigned PUTs/GETs with a separate
   real-network `httpx.AsyncClient()`. Browsers/curl are unaffected.
6. Files tests need the compose stack UP (postgres 5433 + SeaweedFS 9000):
   they exercise real S3 objects, real dedup, real pagination.
7. `tests/test_smoke.py`: 6 tests — healthz shape, index links, OpenAPI
   contains `/healthz`, 404 envelope carries request_id, readyz is 503 and
   names redis as `down` (redis client exists but ping fails), and
   client-supplied request-id is echoed.

## Workflow 9 — Docker image build (Dockerfile)

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

## Workflow 10 — `docker compose up` (local stack)

| Service | Image | Port | Notes |
|---|---|---|---|
| postgres | `pgvector/pgvector:pg16` | **5433**→5432 | pgvector build of PG16 (needed P1); host 5432 occupied by stale stage-1 container `beta-db-1` |
| redis | `redis:7-alpine` | 6379 | AOF persistence on |
| s3 | `chrislusf/seaweedfs:3.99` | 9000→8333, 9333 | S3 API + master UI; creds from `deploy/seaweedfs/s3.json` |
| api | built (`target: api`) | 8000 | env overrides `.env` (env vars beat dotenv in pydantic-settings); `S3_ENDPOINT_URL=s3:8333` (internal) + `S3_PUBLIC_ENDPOINT_URL=localhost:9000` (handed-out presigned URLs) |
| worker | built (`target: worker`) | — | P4: ARQ pipeline + sweep; shares redis db 0 with the api; `restart: unless-stopped` |

`depends_on` uses `service_healthy` conditions, not just "started".

**Substitution note:** MinIO community edition was discontinued mid-2025+
(archived repo, images removed from Docker Hub/quay, binaries 410 Gone).
SeaweedFS is the user-approved local S3 backend; production uses S3/R2 via
aioboto3 — identical code path (S3 API), only the dev container differs.

## Workflow 11 — Testing UI (Phase 3.5, `web/`)

Zero backend changes — the UI is a pure consumer of the P2/P3 contracts. Run:
`npm run dev` in `web/` (Vite on :5173, the origin whitelisted by API CORS and
the SeaweedFS bucket CORS). `npm run build` = strict `tsc` + Vite bundle.

**Session lifecycle** (`web/src/api/session.ts` + `client.ts` +
`state/AuthContext.tsx`):

1. On load, if localStorage has tokens → `me()` (which auto-refreshes on 401)
   → main screen; dead session → login screen.
2. `api()` wrapper: every non-2xx body is the backend envelope → `ApiError`
   (status, code, message, details, request_id). On 401 with a refresh token
   present: ONE shared `refreshInFlight` promise (parallel 401s must not burn
   several single-use rotation tokens) → on success the NEW pair is persisted
   (rotation!) and the request is replayed exactly once (`_retried` flag
   bounds the loop) → on failure `session.clear()` → AuthContext flips to the
   login screen.
3. Logout: POST `/auth/logout` with the refresh token in the body → always
   204 → tokens cleared.

**Upload flow** (`components/Dropzone.tsx` → `components/UploadQueue.tsx`):

1. Drop files or a FOLDER: entries are read synchronously inside the drop
   handler (the DataTransferItemList dies after the microtask), then walked
   recursively (`readEntries` looped until an empty batch — it returns in
   pages of ~100). No entries (synthetic drop, plain files) → fall back to
   `dataTransfer.files`.
2. Each file joins the queue: client-side pre-checks — **0-byte → `file_empty`
   and > 25 MB → `file_too_large` both fail instantly with ONE deduped toast
   per batch** (no pointless network round trip per junk file; the server's
   422 `details.max_bytes` stays authoritative), else `waiting`.
3. Concurrency 3: per file `POST /files/init` (201) → **XHR** PUT of the raw
   File to the presigned URL with `upload.progress` events (0–90% bar) →
   `POST /files/{id}/complete` (200) → `done`, or `♻️ dedup` when the
   response's `deduplicated: true`. XHR (not fetch) is the only browser
   primitive with upload progress + abort.
4. Cancel works at BOTH stages: a not-yet-claimed (`waiting`) item is marked
   `canceled` directly; a claimed item's `AbortController` fires `xhr.abort()`
   → item marked `canceled`. A cancel clicked while `/files/init` is still in
   flight is honored by a post-await `signal.aborted` check (the abort listener
   can't exist before the XHR does). No Cancel button is offered during
   `finalizing` — bytes are already stored, so a cancel there would silently
   do nothing. The orphan init row (never completed) is swept by the P4
   worker by design.
5. `Retry failed` re-queues only `failed` items; `Clear finished` drops
   done/dedup/canceled rows. Every successful complete bumps the grid's
   `refreshSignal` → page 1 refetch.

**Browse** (`components/FileGrid.tsx`): files are grouped into **kind folders**
(Images / PDFs / Documents / Text / Other — collapsible, open by default, with
live counts; the grouping is the primary organization, kind/status/topic
dropdowns remain for power use) built over the real keyset pagination
(`next_cursor` passed back verbatim, "Load more" appends into folders). A
**seq guard** makes a stale in-flight response a no-op (rapid filter flips
otherwise let the last-ARRIVING response win, not the last-ISSUED request);
`refreshSignal` bumps are debounced 400 ms so a big upload drain coalesces
into one page-1 refetch. **View** opens the tab SYNCHRONOUSLY inside the
click handler (`window.open("about:blank")`, then navigated once the
presigned URL arrives — any await before `window.open` lets transient user
activation expire and the popup is blocked SILENTLY; a hard-blocked popup
falls back to an anchor click). **Download** fetches the presigned bytes into
a blob and hands them to the browser via an object-URL anchor with the
ORIGINAL filename — verified live: SeaweedFS ignores the signed
`response-content-disposition=attachment` override and serves `inline`, so a
plain anchor click used to NAVIGATE the app tab away (Chrome rendered PDFs
inline and the whole UI "disappeared"). Rename = double-click → inline form →
`PATCH`. Delete = `window.confirm` → `DELETE` → row removed locally.

**Test aids**: `StatusStrip` polls the PUBLIC `/readyz` every 15s and shows a
dot per dependency + "all up"/"degraded"/"API unreachable". `Toaster` renders
429s with a live countdown built from `details.retry_after_seconds`.

## Workflow 11.1 — UI stability + folders (bug-fix round, `web/`)

Zero backend changes. Three user-reported bugs — "can't view files after
uploading", "UI shakes up and down when an upload fails", "files should be in
folders, not manual filters" — plus everything a stress reproduction
surfaced.

**Root cause of the shake**: dropping a big folder put ~2,000 queue rows in
the DOM, each re-rendering on every XHR progress tick (~30/s), while every
completion refetched the grid and every failure appended a toast — the page
jumped around continuously (measured layout-shift total 3.49 for one drain).

**Fixes (all measured, not guessed)**:

- **Bounded swap-strip queue** (`UploadQueue.tsx`): 5 equal-height slot rows
  whose CONTENT swaps as files drain — rows are never removed mid-drain. The
  slot count grows with demand but never shrinks while the queue is visible
  (high-water `slotFloorRef`, reset when the queue fully empties), so a
  1-file upload shows 1 row, not 5 blank ones. Failed rows capped at 20
  ("…and N more failed"); finished items collapse into a counts line
  (`✅ N done · ♻️ N dup · 🚫 N canceled` + Clear). Reserved heights
  everywhere: `.qi-bar.idle`, `.queue-group-label.idle`, `.queue-actions.idle`,
  `.queue-more.idle` — visibility toggles, existence never does. Progress
  patches throttled to ~8/s; rows memoized. Layout-shift total for the same
  drain: **3.49 → 0.19** (the residue is one-time content arrival).
- **Toast dedupe + stable anchoring** (`Toaster.tsx`): identical events
  (kind+title+detail, request_id suffix stripped from the key) BUMP one row
  (×N badge + timer restart) instead of stacking — a 45-file failing batch
  produces ONE toast, not 45. The authoritative list is a ref mutated
  synchronously in the handler (a render-mirror lags behind synchronous
  bursts). Newest-first DOM in a bottom-anchored column: add/evict/timeout
  move nothing already on screen. Cap 5 rows, 6s timers.
- **Sync-open View** (`FileGrid.tsx`): `window.open` first, fetch second —
  see Workflow 11's Browse note.
- **Blob download**: SeaweedFS ignores `response-content-disposition`
  (verified) — see Workflow 11's Browse note.
- **Instant client pre-checks**: oversize AND empty files fail with a deduped
  toast at drop time; identical server rejections (e.g. after Retry failed)
  dedupe too because the request_id suffix is stripped from the toast key.

**Verification** (automation browser, real stack): view (text/png/docx,
popup-block fallback), toast dedupe (×5/×6 bump, byte-identical row
positions), 60-file mixed drains (bounded DOM, dedup badges, cancel counts),
cancel during init (zero PUTs, zero completes) and on a waiting item (never
claimed), UI delete with the real confirm dialog, rename, filters,
collapse/expand, reload persistence, zero console errors. Backend gates
twice: ruff + mypy + pytest + p3_live_check (29/29) + p4_live_check (10/10).

## Workflow 12 — Queue + worker (Phase 4)

**Design (user-locked: enqueue + sweep, full skeleton, 3 tries / exp backoff):**

```
complete_upload (processable kind)
   └─ commit (status=uploaded) ─▶ enqueue_job("process_file", id, _job_id="process:{id}")
                                   │ best-effort: Redis down → log only
worker cron (every 30s)          ▼
   sweep_uploads ──▶ status=uploaded + sha256 NOT NULL ─▶ enqueue (same job_id → arq dedups)
                 ──▶ sha256 NULL + created_at < 60min ago ─▶ DELETE row (orphan init)
process_file:
   uploaded ─claim─▶ processing ──▶ _run_pipeline() ──▶ done
                          │        (P4: no-op seam; P5/P6 replace body only)
                          └─ raise ─▶ attempt < 3: Retry(defer=5·2^(n-1) capped 300)
                                    attempt = 3: status=failed + error text (browsable)
```

**Click → code path** (worker side):

1. `POST /files/{id}/complete` commits the processable row →
   `FileService._enqueue_process` (`app/services/files.py`) enqueues with the
   canonical job id `process:{file_id}` — arq collapses duplicates by job_id
   while the job exists. Enqueue failure is swallowed BY DESIGN: the row is
   already committed and the sweep re-enqueues it within 30s.
2. The worker (`arq app.workers.settings.WorkerSettings`, compose `worker`
   target) pops the job → `process_file` (`app/workers/pipeline.py`):
   - row missing → log + return (user deleted during a backoff window);
   - sha256 NULL → never claim (defensive — the sweep also excludes these);
   - `status=done|failed` → return "skipped" (idempotency guard: retries and
     duplicate enqueues never re-process);
   - `status=uploaded` → claim: `processing`, commit (crash-safe: a dead
     worker's in-flight job is re-run by arq's `retry_jobs` on startup, and
     re-entry into a claimed file is safe — the seam is idempotent);
   - `_run_pipeline(file)` — **the P5/P6 seam** (P4 body: no-op; doc_topic
     stays NULL for processable kinds until P6 assigns real topics);
   - exception: `ctx["job_try"] < WORKER_MAX_TRIES` → `Retry(defer=5·2^(n-1))`
     (5s, 10s, 20s … capped 300s); else terminal → `status=failed` +
     `error[:500]` (the UI shows the failed chip).
3. `sweep_uploads` (cron `second={0,30}`, `run_at_startup`, `unique`,
   `max_tries=1`): enqueue candidates (uploaded + sha256 set) with the same
   job_id → already-queued jobs dedup; orphan inits (sha256 NULL older than
   `ORPHAN_INIT_MINUTES`) deleted — no S3 cleanup needed (no PUT, no object).

**Why two trigger paths**: the enqueue gives instant processing in the happy
path; the sweep closes the gap where the API died between its DB commit and
its Redis enqueue (or Redis was down — the limiter is fail-open, the queue
cannot be). Together: no completed file ever waits longer than one sweep.

**Redis isolation**: API enqueues on `REDIS_URL` (compose: db 0); conftest
forces **db 15** for tests so a running dev worker can never consume jobs the
tests enqueue, and direct-call worker tests can assert queue state without a
live worker racing them.

**Technologies**: arq 0.28 (Redis asyncio job queue; sorted-set queue, job
hashes, `retry_jobs=True` startup re-queue for dead-worker recovery),
SQLAlchemy `delete()` + rowcount for the orphan sweep, structlog JSON in the
worker process (same configure_logging as the API — every worker line is
JSON with request_id="-").

## Technologies in play (through Phase 4)

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
- **aioboto3** (async boto3 + aiobotocore) — the only S3 client: SigV4
  presigned PUT/GET URLs, HeadObject/GetObject streaming, DeleteObject,
  HeadBucket readiness, PutBucketCors bootstrap. Dual-endpoint design
  (internal client vs public presigner) keeps container networking honest.
- **arq 0.28** — Redis-backed asyncio job queue: enqueue-on-complete +
  cron sweep (crash recovery), per-job idempotency via `_job_id`, `Retry`
  exponential backoff, `retry_jobs=True` for dead-worker startup recovery.
- **uv** — locked, cached dependency installs (host + Docker builder).
- **Docker Compose** — 4-service local stack with health-gated startup.
- **React 18 + Vite 5 + TypeScript 5.6 (strict)** — `web/` testing UI; zero
  runtime deps beyond react/react-dom; XHR for upload progress; localStorage
  token store; CSS hand-rolled (no framework).
