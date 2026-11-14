# PROGRESS — smart-file-handler

Running log of phases, verification evidence, and known gaps. Updated after every checkpoint.

## Locked decisions (user-confirmed, do not change silently)

| # | Decision |
|---|---|
| 1 | Rebuild stage-1 repo into the spec's target structure; reuse ideas (JWT/refresh shape, naming conventions), rewrite files |
| 2 | Face ML: `insightface` pip package + buffalo_l, worker container only |
| 3 | Zero-shot ML: `open_clip` + torch (ViT-B-32) |
| 4 | **Processing matrix A**: jpg/jpeg/png/webp → faces + CLIP things · pdf → PyMuPDF text + topic · docx/pptx/xlsx → office text + topic · txt/md/csv → direct text + topic · **everything else → kind=other, doc_topic="other", status=done, browsable in All Files** |
| 5 | Max upload 25 MB default (env `MAX_UPLOAD_MB`) |
| 6 | Topic syllabus A–O documented in `docs/topics/*.md` per phase |
| 7 | Git: commit per checkpoint; user pushes to origin |
| 8 | Build at repo root (`fin-sm-f/`); emails lowercased (no citext); face cosine threshold 0.6 (env-configurable); SSE deferred; storage key `{user_id}/{file_id}{ext}` |
| 9 | **Local S3 backend = SeaweedFS** (`chrislusf/seaweedfs:3.99`), user-approved after MinIO community edition was discontinued (repo archived, images gone from Docker Hub/quay, dl.min.io binaries 410 Gone — all verified). Production stays S3/R2 via aioboto3. |
| 10 | **`docs/WORKFLOW.md`** — single living workflow doc (click → function → file → algorithm → technology), updated after every phase. User-requested. |
| 11 | **P3 upload design (9 decisions via Q&A)**: presigned init/complete (bytes bypass API); duplicate → EXISTING row + `deduplicated: true` + duplicate object deleted; hard delete (row + S3 object); thumbnails deferred to P5; P3 browse = filters only; **sniff wins** (fake .jpg → other); office = ZIP magic + extension; PATCH rename; 20/page max 100, newest-first, opaque base64 cursor. |
| 12 | **P3 endpoint split**: `S3_PUBLIC_ENDPOINT_URL` for handed-out presigned URLs (host-reachable) vs `S3_ENDPOINT_URL` for container ops; SigV4 only (v2 breaks on client Content-Type); bucket CORS bootstrapped from `CORS_ORIGINS` at startup. |
| 13 | **P3.5 UI (via plan-mode Q&A)**: React + Vite + TS in `web/` (same repo); tokens in localStorage + auto-refresh on 401 (reload keeps the session); full upload queue — drag & drop incl. folder drop, per-file XHR progress + cancel, retry-failed-only, dedup + oversize badges; test-aids status strip (live /readyz dots + 429 countdown); **zero backend changes**. |

## Phases

| Phase | Scope | Status |
|---|---|---|
| P0 | Skeleton: config, logging, errors, app factory, health, compose (pgvector-pg16, redis, SeaweedFS S3, api), Dockerfile | ✅ done (verified 2026-11-12) |
| P1 | DB + Alembic async: models, initial migration + vector extension | ✅ done (verified 2026-11-12) |
| P2 | Auth: register/login/refresh rotation/logout, rate limiting | ✅ done (verified 2026-11-12) |
| P3 | Upload flow: presigned init/complete, dedup, validation, keyset pagination | ✅ done (verified 2026-11-13) |
| P3.5 | React testing UI in `web/`: auth + auto-refresh, presigned upload queue (progress/cancel/retry/dedup badges), filter grid | ✅ done (verified 2026-11-14) |
| P4 | Queue + worker skeleton: ARQ, idempotency, retry/backoff, crash recovery | pending |
| P5 | Image pipeline: InsightFace faces, pgvector clustering, CLIP labels | pending |
| P6 | PDF/Office/text pipelines: extraction + topic labels | pending |
| P7 | Browse + curate APIs: people/labels, rename/merge/unassign/delete | pending |
| P8 | Hardening: RED metrics, Sentry, bomb guards, EXPLAIN audits | pending |
| P9 | Deploy: prod compose, CI, README, WHY.md, .env.example final | pending |

## P0 — Skeleton ✅

Built: `app/core/{config,logging,errors}.py`, `app/main.py` (factory + module app),
`app/api/routers/health.py` (`/healthz`, `/readyz`, `/metrics`, root index),
`Dockerfile` (multi-stage, `api` + `worker` targets), `docker-compose.yml`
(pgvector-pg16, redis, SeaweedFS-as-S3, api), smoke tests, full `.env.example`,
`deploy/seaweedfs/s3.json` (S3 credentials), `docs/WORKFLOW.md` (living
workflow doc, user-requested, updated every phase).

Verified (2 full passes as required):

- **Pass 1 (warm)**: `compose up` → all 4 services healthy; `/healthz` 200;
  `/readyz` 200 `{"checks":{"redis":"up"}}`; `/metrics` 200 (prometheus
  exposition); `/docs` 200; `/openapi.json` 200; `GET /nope` → 404 envelope
  `{"error":{"code":"http_error",...},"request_id":...}`; client-supplied
  request-id echoed in header; `pytest` 6/6 green; api logs audited — every
  line JSON with `request_id`.
- **Pass 2 (cold)**: `compose down -v` (wiped volumes) → fresh `compose up
  --build` → identical results; `pytest` 6/6 again.

Fixes made during verification (caught by testing, per the "question every
line" rule):

1. `/metrics` was a mounted ASGI app → 307 redirect to `/metrics/` (empty body
   in curl). Replaced with explicit route using `generate_latest()`.
2. SeaweedFS binds only its container IP by default → healthcheck
   `wget localhost:9333` got connection refused. Fixed with `-ip.bind=0.0.0.0`.
3. `minio/minio` image gone (Docker Hub), quay.io 401, dl.min.io 410 —
   MinIO community discontinued; switched to SeaweedFS (user-approved).
4. Host port 5432 occupied by stale stage-1 container `beta-db-1` →
   postgres mapped to host **5433** (host-side DATABASE_URL updated too).
5. pytest needed `pythonpath=["."]` (app repo, package=false); lifespan run
   explicitly in the `client` fixture (httpx ASGITransport skips it).

## Known gaps (carried, resolved in later phases)

- ~~S3 (SeaweedFS) readiness check lands in P3~~ **done in P3** (readyz s3:up).
- Orphan init rows (init without complete) linger; cleanup sweeps land in P4.
- Dockerfile `worker` target exists but inert (no `app.workers` until P4).
- `/metrics` exposes default process metrics only; RED HTTP metrics land in P8.
- README still stage-1 placeholder; rewritten in P9 with architecture/scaling docs.
- ML deps (insightface, open_clip, torch, PyMuPDF, office parsers) added in P5/P6.
- Integration tests require the compose stack running + migrated (documented).
- Rate limiter trusts `request.client.host` only; X-Forwarded-For handling
  (proxy-aware real IPs) lands in P9 deployment.
- `files.status` filter matches only exact enum values; combined
  kind+status+topic queries are ANDed (fine for P3; search arrives later).

## P1 — DB + Alembic ✅

Built: `app/db/base.py` (naming conventions + CreatedAtMixin), `app/db/session.py`
(async engine, `session_factory`, `get_session` dependency, `expire_on_commit=False`),
7 models in `app/db/models/` (User, RefreshToken, File, Person, FaceInstance,
Label, FileLabel) with pgvector `Vector(512)` columns, async `alembic/env.py`,
initial migration `20261112_bbecbe6aa8ec_initial_schema_with_pgvector.py`
(hand-fixed: `CREATE EXTENSION vector`, `DROP TYPE`s, VECTOR import),
`tests/test_db_integration.py` (pgvector roundtrip + cosine NN + enum values),
`/readyz` now checks DB too, lifespan disposes the engine on shutdown.

Verified (2 passes):

- Pass 1: `upgrade head` → `downgrade base` → `upgrade head` all clean;
  psql introspection: extension `vector 0.8.7`, 8 tables (incl. alembic_version),
  `ix_face_instances_embedding_hnsw` present, 10 FKs with correct ondelete
  (CASCADE ownership chains; SET NULL for person_id + replaced_by_id),
  3 native enum types; pytest 8/8 (cosine NN roundtrip proves the
  SQLAlchemy ↔ pgvector ↔ asyncpg path end to end).
- Pass 2: second `downgrade base` → `upgrade head` cycle clean; rebuilt api
  image (first attempt revealed the stale P0 container serving old readyz!) →
  live `/readyz` `{"redis":"up","db":"up"}` 200; healthz/metrics/docs/404
  re-verified; pytest 8/8 again.

Bugs caught by testing this phase:

1. Models had no `default=uuid.uuid4` on PKs → NULL-id NotNullViolation on
   insert (pgvector roundtrip test caught it). Fixed in all 5 PK tables.
2. conftest still pointed DATABASE_URL at 5432 (stale beta-db-1) while .env
   used 5433 → password auth failed in tests only. Synced to 5433.
3. Cross-test event-loop race: global engine pooled connections bound to a
   dead loop (proactor `NoneType.send`). Test db fixture now builds a
   NullPool engine per test and disposes it; prod pooling untouched.
4. `alembic.exe` console-script shim broken on this machine → always run
   `python -m alembic` via the venv.
5. Autogenerated migration referenced pgvector's VECTOR without importing it
   and never dropped enum types in downgrade — hand-fixed.
6. Stale api container kept serving the P0 image after code changes → rebuilt.

## P2 — Auth ✅

Built: the three-layer structure the rest of the app follows.

- `app/schemas/auth.py` — request/response models (SecretStr passwords,
  EmailStr, TokenPair).
- `app/repositories/` — `users.py`, `refresh_tokens.py`; all queries live
  here, services never build SQL.
- `app/services/security.py` — argon2id hashing (64 MiB/3 iters/4 lanes),
  HS256 JWT access tokens (typ=access, sub=uuid), 256-bit opaque refresh
  tokens stored only as SHA-256, dummy-verify for timing-equalized
  "unknown email" responses.
- `app/services/auth.py` — register (dup → 409, IntegrityError race caught),
  login (generic `invalid_credentials` 401 both paths), refresh with
  **rotation** (old → revoked + replaced_by_id chain) and **reuse detection**
  (a revoked token presented again nukes the user's ENTIRE live token family),
  logout — **idempotent since P2.1: always 204** (live → revoked; unknown/
  revoked → no-op; tokens stripped).
- `app/services/rate_limit.py` — Redis fixed-window limiter (INCR+EXPIRE),
  shared across replicas, **fail-open** if Redis is down (availability first,
  argon2 cost still throttles), 429 + Retry-After.
- `app/api/deps.py` — `get_current_user` (Bearer → JWT → user row; 401 for
  missing/garbage/unknown-user), `rate_limit(scope, limit)` dependency
  factory, `auth_service` provider.
- `app/api/routers/auth.py` — POST /auth/register (201), /auth/login,
  /auth/refresh, /auth/logout (204), GET /auth/me.
- `app/core/errors.py` — AppError now carries `headers` (429 → Retry-After).
- Tests: `tests/test_auth.py` (17 tests: register happy/dup/invalid/short,
  login happy/wrong-pw/unknown-email-identical-response, me guard, rotation +
  reuse nuke + unknown token, logout + oracle guard, login + register 429).
  conftest now uses the real compose Redis with a unique-per-run
  RATELIMIT_NAMESPACE; readyz-down test builds its own dead-Redis app.

Verified (2 passes):

- Pass 1: pytest 25/25 (8 prior + 17 auth), ruff + mypy clean.
- Pass 2: rebuilt api image → live curl walkthrough: register 201 → login
  pair → /auth/me 200 → refresh rotation (new pair, chain audited in DB) →
  old-token reuse 401 → family nuke proven (even the fresh token died) →
  fresh login + logout 204 → wrong password 401 generic → missing bearer 401
  → 11 rapid logins: 401s then 429 with `retry-after: 34`.
  DB audit: refresh_tokens rows revoked + replaced_by_id set.

Decisions made this phase (standard, flag if you want changes):

- Password floor 8 chars (pydantic min_length) — argon2 does the real work.
- Refresh tokens live 7 days (config REFRESH_TOKEN_EXPIRE_DAYS).
- ~~Logout with unknown/revoked token → 401~~ **changed in P2.1** (below).
- After logout, the access token still works ≤15 min (stateless JWT; short
  expiry + rotation is the bound — no token blacklist).
- Rate limiter fail-open on Redis outage.

## P2.1 — Logout fix (user-reported) ✅

Bug: after exercising `refresh` (or hitting reuse detection), `logout` with
the held token returned 401 `invalid_refresh_token` — the client could not
log out at all. Root cause: strict-401 design + single-use rotation means
every token a client still holds is already dead.

Fixes (user confirmed the design via plan-mode Q&A):

1. **Idempotent logout (RFC 7009)**: `POST /auth/logout` always 204 — live
   token → revoked; unknown/revoked → silent no-op. Kills the token-existence
   probing oracle too (uniform response).
2. **`.strip()` on presented tokens** in refresh + logout (copy-paste
   whitespace used to change the SHA-256 and fail a correct token).
3. Clearer 401: "Invalid or expired refresh token. Please log in again."
4. **Human 429 body** (user request): "Too many login attempts. You can try
   again in 28 seconds." + `details.retry_after_seconds`; scope-specific
   wording (login/registration); `Retry-After` header retained.

Verified 2×: pytest 26/26 (idempotent-logout + whitespace + 429-body tests),
ruff + mypy clean; rebuilt image → live curl: stale-token logout 204
(the user's exact scenario), live-token 204, repeat-logout 204, garbage 204,
whitespace-padded refresh 200, 429 body message with correct seconds.

## P3 — Files: presigned upload, dedup, browse ✅

Built (7 routes under `/files`, all Bearer-protected):

- `app/services/storage.py` — the ONLY module touching object storage.
  aioboto3 + `AioConfig(signature_version="s3v4")` (v4 presigned URLs sign
  only the host header → client Content-Type can't invalidate them; v2 is
  deprecated). Dual endpoint: internal client (`S3_ENDPOINT_URL`, container
  ops: head/read/delete) vs presign-only client (`S3_PUBLIC_ENDPOINT_URL`,
  URLs handed to clients — host-reachable). `ensure_bucket` = HeadBucket →
  CreateBucket (2-replica race caught) → PutBucketCors from `CORS_ORIGINS`.
- `app/services/sniffing.py` — pure byte classifier (no I/O): JPEG/PNG/WEBP/
  PDF magics; ZIP magic + extension → docx/pptx/xlsx or `other`; text kinds
  must decode UTF-8 with no NULs; everything else `other` — **sniff wins
  over the filename** (fake .jpg never enters the face pipeline).
- `app/services/files.py` — init (size cap, sanitize, key
  `{user_id}/{file_id}{ext}`, presign_put, row w/ sha256=NULL) / complete
  (HEAD → size check → sniff → stream SHA-256 → per-user dedup → commit) /
  list (keyset) / get / url / rename / delete (S3-before-DB ordering).
- `app/repositories/files.py` — all file SQL + keyset cursor:
  base64url `{created_at}|{id}`, PG row-value `(created_at,id) < (c1,c2)`
  on `ix_files_user_created_id`, `limit+1` has_more probe.
- `app/schemas/files.py`, `app/api/routers/files.py` — shapes only.
- `app/api/deps.py` — `get_storage` (lifespan app.state) + `files_service`.
- `app/main.py` — lifespan starts/closes StorageService; files router.
- `/readyz` — `s3: up/down` via HeadBucket.
- Migration `20261113_a3f1c2d4e5f6`: `files.sha256` nullable +
  `uq_files_user_id_sha256` (NULLS DISTINCT → pending uploads don't collide).
- `tests/test_files.py` — 19 tests vs REAL SeaweedFS: init row+presign,
  oversize/zero-size 422, kinds jpeg/png/webp/pdf/docx/txt/csv, bat→other,
  fake-jpg sniff-wins, 409 upload_not_found, idempotent complete, dedup
  returns existing row (DB count=1), pagination+cursor, filters+bad enum
  422, bad cursor 422, cross-user 404 matrix, view/download url, rename,
  delete removes row+object (presigned GET then != 200), auth guard,
  **presign public-endpoint test**.
- `scripts/p3_live_check.ps1` — repeatable 29-check live walkthrough.

Decisions locked with the user (plan-mode Q&A): presigned init/complete
(bytes bypass API); duplicate upload returns EXISTING row + `deduplicated:
true` + duplicate object deleted; hard delete (row + S3 object); thumbnails
deferred to P5; browse = filters only (no search yet); sniff wins; office =
ZIP magic + extension disambiguation; PATCH rename; 20/page (max 100),
newest-first, opaque cursor.

Verified (2 full passes as required):

- Pass 1: ruff + mypy clean (41 files), pytest **46/46** (8 smoke/db + 18
  auth + 19 files + 1 presign-endpoint).
- Pass 2: identical full gate again.
- Live ×2 (rebuilt image): `scripts/p3_live_check.ps1` **29/29** — readyz
  s3:up; register/login; init→PUT→complete for all 7 kinds; fake .jpg →
  other+done; dedup `deduplicated:true` + original id; 409 upload_not_found;
  422 file_too_large; newest-first list + cursor page 2; kind/doc_topic
  filters; bad kind 422; cross-user GET/DELETE 404; presigned GET serves
  exact bytes; download disposition; PATCH rename; DELETE 204 → GET 404 →
  S3 object gone; **CORS preflight 200 with Access-Control-Allow-Origin**
  (browser PUT verified end-to-end against SeaweedFS).

Bugs caught by testing this phase:

1. `FileCompleteOut.model_validate(File)` 500'd — ORM has no `deduplicated`
   attribute. Fixed: build `FileOut` then compose explicitly.
2. Test-harness illusion: httpx `ASGITransport` routes absolute URLs into
   the app — "S3 PUT 404" was OUR envelope. Real-network client for S3 ops
   in tests.
3. **Presigned URLs used the container-internal endpoint (`s3:8333`)** —
   unresolvable from host browsers/curl (tests passed only because the app
   ran in-process with host env). Added `S3_PUBLIC_ENDPOINT_URL` +
   presign-only client. This was the big one — a production bug class.
4. SigV2 presigned signatures break when the client sends any Content-Type
   (v2 signs it; PowerShell/browser always do). Migrated to SigV4
   (host-only signed) — verified 200 with unrelated Content-Type.
5. SeaweedFS answered browser CORS preflights with 403 AccessDenied
   (anonymous OPTIONS hit the IAM layer). Fixed: PutBucketCors at startup
   from `CORS_ORIGINS` — preflight now 200 with the right headers.
6. Test pollution ×2: DB rows persist across runs → fixed payload counted
   previous runs' rows (dedup test now uses unique bytes); rate-limit
   counters leaked across test modules (flush fixture moved to conftest).
7. Invalid `?kind=nope` → ValueError 500; now mapped to 422 `bad_filter`.
8. mypy: aioboto3 needs `AioConfig(**kwargs)`, NOT `AioConfig(botocore.Config)`
   (lands in connector_args → `.items()` AttributeError at runtime).

Known gaps from P3 (carried): orphan init rows (init-without-complete)
cleanup lands in P4; complete's single-pass hash+sniff is fine at 25 MB but
multipart/large-object support would need range reads (P9 decision);
`status` filter is exact-match only.

## P3.5 — React testing UI (`web/`) ✅

Built (zero backend changes — pure consumer of the P2/P3 contracts):

- Scaffold: `web/package.json` (react 18 + react-dom only), `vite.config.ts`
  (dev server :5173 — the whitelisted CORS origin), `tsconfig.json` (strict:
  noUnusedLocals/Parameters, noFallthrough), `index.html`, `.gitignore`.
- `src/types.ts` — TS mirrors of the Pydantic schemas + error envelope +
  `/readyz` body (manual sync; a generated client was overkill).
- `src/api/session.ts` — localStorage token store with a listener registry
  (the fetch client can wipe a dead session; AuthContext reacts).
- `src/api/client.ts` — fetch wrapper: bearer header, envelope → `ApiError`
  (status/code/details/request_id), **single-flight 401 → refresh → replay
  once** (`_retried` bound; parallel 401s share one refresh because rotation
  tokens are single-use; dead refresh → `session.clear()` → login screen).
- `src/api/{auth,files,upload}.ts` — endpoint callers; `upload.ts` is the
  **XHR PUT** to presigned URLs (only browser primitive with upload progress
  + abort; no auth header — the SigV4 signature is the authorization).
- `src/state/AuthContext.tsx` — hydrate on load (`me()`), login/register/
  logout, external-clear subscription.
- `src/components/` — `AuthScreens` (login/register + envelope errors),
  `StatusStrip` (public `/readyz` poll → 3 dots + "all up"), `Toaster`
  (event-based; 429 shows a live countdown from `details.retry_after_seconds`),
  `Dropzone` (files AND folders: entries grabbed synchronously, walked
  recursively with batched `readEntries`; synthetic-drop fallback),
  `UploadQueue` (`useUploadQueue` hook + view: concurrency 3, XHR progress
  bar, cancel via AbortController, retry-failed-only, clear-finished, 25 MB
  client-side pre-check → oversize badge), `FileGrid` (cards over real keyset
  pagination + kind/status/topic filters + stale-response seq guard, view/
  download via presigned URLs, double-click rename, confirm-dialog delete).
- `src/App.tsx`/`main.tsx`/`styles.css` — dark shell, queue hoisted so the
  Dropzone feeds it; every successful complete bumps the grid refresh.

Verified (2 full passes as required):

- Pass 1 (2026-11-13, browser): register via UI form (201) → main screen with
  status strip "all up" (live readyz polling); synthetic DragEvent with 7
  magic-byted files (jpg/png/pdf/docx/txt/csv/bat) → **all 7 uploaded**: init
  201×7 → presigned PUT 200×7 → complete 200×7; queue showed per-file done
  states + "Clear finished (7)"; network log confirmed the full contract
  incl. CORS preflights; zero console errors; `npm run build` clean (161 KB
  JS gzip 52 KB).
- Pass 2 (2026-11-14, browser + backend gates): dedup re-drop of photo.jpg →
  ♻️ "duplicate: linked to the existing file" (complete 200 `deduplicated`).
  Filter matrix ×5: kind=image → 2; image+status=done → none; status=done →
  setup.bat only; topic=other → setup.bat only; reset → all. Rename via
  double-click → PATCH 200 → card updated (+renamed back). Delete → confirm
  dialog → row gone **server-side** (6 files remain). Logout → tokens
  cleared → reload stays logged out → login again → reload → session
  persists (auto-refresh path). Backend regression: ruff ✓ mypy ✓ (41 files),
  pytest **46/46**, `scripts/p3_live_check.ps1` **29/29**.

Bugs caught by testing this phase:

1. **FileGrid stale-response race** (found live): rapid filter changes left
   several fetches in flight; the last-ARRIVING response won regardless of
   which request was issued last (filter results flickered wrong). Fixed
   with a monotonic seq guard — a superseded fetch may not touch state.
2. Dropzone synthetic-drop hole: when `webkitGetAsEntry` existed but returned
   null for every item (scripted drops), files were silently dropped.
   Rewritten to an entries-length check with `dataTransfer.files` fallback.
3. client.ts 401-replay could loop in principle on a fresh-but-rejected
   token; bounded with the `_retried` flag (exactly one replay).
4. `npm audit`: 2 dev-advisories inside the Vite 5 toolchain (build-time
   only, not shipped code); Vite 6/7 upgrade deferred — noted, not blocking.

Known gaps from P3.5 (carried): oversize badge (>25 MB) verified by code
reading + server 422 test only — `browser.files.drop` caps at 5 MiB so the
real >25 MB drop is a manual test item; orphan init rows from canceled
uploads await the P4 sweep; `web/` not Dockerized (dev-only UI until a P9
decision on serving the build).
