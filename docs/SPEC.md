# OpenCode prompts: migrate smart-file-handler to v3 (Drive-style + sharing)

## How to use this

1. Save your full v3 master context doc into the repo as `docs/SPEC.md`. (The copy you pasted to me was cut off in the middle of section 10, flow 6, so make sure your saved copy is complete, including sections 11-16.)
2. Paste **PROMPT 0** once. It makes OpenCode write `AGENTS.md`, which OpenCode loads automatically every session. That way a small local model never has to hold the whole spec in one prompt.
3. Then paste **one phase prompt at a time**. Don't move on until the checkpoint passes and you've read the diff.
4. After each phase: read every changed file, and write one line per function on what it does and why. Anything you can't explain goes on your learning list.
5. Memory note: use `ML_STUB=1` for most phases. Don't run Ollama, the full stack and Locust at the same time.

---

## PROMPT 0: Bootstrap + audit (no code changes)

```
You are a senior backend engineer migrating an existing repo (smart-file-handler) to a new design.

Step 1. Read docs/SPEC.md fully. It is the single source of truth. If anything is ambiguous or conflicts with the current repo, list it, don't silently decide.

Step 2. Create AGENTS.md in the repo root containing a SHORT (under 120 lines) operating manual for future sessions:
- one-paragraph project summary
- the stack
- architecture rules (layering, repositories-only-build-queries, authz-via-AccessService returning 404 on no access, no dual writes/outbox, idempotent state-machine jobs, keyset pagination, stateless API, no blocking in async, config via env)
- "CUT" list from the spec (do not reintroduce)
- STRETCH list (do not build unless I ask)
- workflow rules: work on ONE phase at a time, stop at the checkpoint, run tests, update PROGRESS.md, make one git commit per phase, never delete data or drop tables without asking me first, never invent library APIs (check the installed version's docs/source when unsure), prefer small files, ask before adding any dependency not in the spec
- the ML_STUB=1 switch convention (workers use a fixed-delay fake instead of real models)
- pointer: "full details in docs/SPEC.md"

Step 3. Audit the current repo against the spec. Do not change code. Write MIGRATION_PLAN.md with:
- what exists and is reusable as-is
- what exists but must change (file by file)
- what exists but is CUT (ARQ, faces, persons, pgvector use, Sentry, SSE, per-user dedup-by-reuse, anything admin-related)
- what is missing
- the migration risk: does a database with real data exist? What is the safest path for the schema change (regenerate initial migration if no real data, else expand/contract)? Ask me this question explicitly at the end.
- the phase list below, adjusted to what you found

Step 4. Create PROGRESS.md with the phase checklist, all unchecked.

Stop after this and wait for my answers.

PHASES: 1 cleanup+skeleton, 2 schema+migrations, 3 auth+rate limiting, 4 folders+AccessService+authz tests, 5 upload flow+quota+idempotency, 6 outbox+relay+Celery+sweeper, 7 scan+image+PDF pipelines, 8 sharing (users+links), 9 search, 10 audit+trash+purge+labels browse, 11 hardening+observability, 12 load test+report, 13 CI/CD+deploy.
```

---

## PHASE 1: Cleanup + skeleton

```
Do Phase 1 only. Read AGENTS.md and the relevant parts of docs/SPEC.md (sections 5-7).

Goal: bring the repo structure and tooling in line with the v3 design, with no feature logic yet.
- Remove everything marked CUT: ARQ, face/person code, pgvector usage, Sentry, SSE, per-user SHA dedup-by-reuse, any admin code. Show me the list of what you remove BEFORE deleting and wait for my OK.
- Restructure to the folder layout in section 7. Create empty modules with a one-line docstring saying their responsibility and what they may depend on.
- core/config.py with pydantic-settings (DB, Redis, S3/MinIO, ClamAV, JWT secrets, limits, ML_STUB). .env.example updated.
- core/logging.py: structlog JSON + request-ID middleware. core/errors.py: domain exceptions + handlers (NotFound -> 404, etc.).
- app/main.py app factory with lifespan; routers/health.py with /healthz (liveness) and /readyz (db, redis, storage).
- Dockerfile multi-stage with `api` and `worker` targets. docker-compose.yml: api, worker, relay (placeholder command), postgres, redis, minio, clamav. Add healthchecks and sensible memory limits (the host has 16 GB RAM).
- pyproject with ruff + mypy + pytest config.

CHECKPOINT: `docker compose up` brings everything healthy; GET /healthz = 200; /readyz reflects real dependency state (stop redis -> 503); ruff, mypy and pytest (one smoke test) pass. Update PROGRESS.md, commit.
```

---

## PHASE 2: Schema + migrations

```
Do Phase 2 only. Read AGENTS.md and SPEC.md sections 8 and 9.

First, based on my answer in MIGRATION_PLAN.md about real data, follow the agreed migration strategy. If you need to destroy or rewrite existing migrations, ask me first.

Implement SQLAlchemy 2.0 models (Mapped/mapped_column) for: users, refresh_tokens, folders, files, labels, file_labels, permissions, share_links, audit_log, outbox, idempotency_keys. Exactly as specified: enums, CHECK constraints (exactly one of file_id/folder_id on permissions and share_links), unique constraints, explicit ON DELETE on every FK, naming conventions in db/base.py, partial indexes (live rows WHERE deleted_at IS NULL, unpublished outbox rows), GIN on search_vector and pg_trgm GIN on file name, generated tsvector column.
Async Alembic env.py. Migration enables pg_trgm (and citext if used).
db/session.py: async engine, async_sessionmaker with expire_on_commit=False, get_session dependency (one session per request).

Write a short docs/adr/0001-folder-tree-adjacency-list.md explaining adjacency list + recursive CTE with depth cap 20 vs ltree, and say it will be benchmarked later.

CHECKPOINT: on an empty DB `alembic upgrade head` then `downgrade base` then `upgrade head` all succeed; `alembic check` shows no drift; a test inserts rows violating each CHECK/unique constraint and asserts the DB rejects them. Update PROGRESS.md, commit.
```

---

## PHASE 3: Auth + rate limiting

```
Do Phase 3 only. Read AGENTS.md and SPEC.md sections 6, 8, 9 (auth parts).

Implement: register (creates user + root folder in one transaction), login, refresh, logout. argon2 hashing. 15-minute access JWT. Refresh tokens stored hashed with family_id, rotation on every refresh, and REUSE DETECTION: presenting an already-rotated token revokes the whole family. get_current_user dependency. Per-user storage quota fields set at registration.
core/ratelimit.py: Redis token bucket via a Lua script (atomic), used on login/register/refresh keyed by IP+email. Return 429 with Retry-After.
Never log tokens or passwords.

Tests: register/login happy path; wrong password and unknown email return the same response; refresh rotation works; reusing an old refresh token revokes the family; rate limit triggers; concurrent register of the same email yields exactly one success.

CHECKPOINT: all tests green. Update PROGRESS.md, commit.
```

---

## PHASE 4: Folders + AccessService + authz tests

```
Do Phase 4 only. Read AGENTS.md and SPEC.md section 9 carefully.

Implement services/access_service.py: resolve_role(actor, resource) -> owner|editor|viewer|None. Role is the highest of: ownership, direct grant on the resource, or a grant on any ancestor folder (recursive CTE with depth cap 20). A trashed ancestor makes the subtree inaccessible except to the owner via trash. All SQL lives in repositories.
Implement folder_service + routers: create, rename, list children (keyset paginated by name or created_at), move, trash, restore. Folder move must reject moving into its own subtree, under pg_advisory_xact_lock on the owner, so concurrent moves can't create cycles.
EVERY service method takes an explicit actor and calls AccessService first. No access -> NotFound (404), never 403.

Tests (put these in tests/authz_matrix/): a table-driven matrix of roles (owner, editor via direct grant, editor via ancestor grant, viewer, stranger, owner-of-trashed-ancestor) x operations (list, read, rename, move, trash, restore) with expected outcomes. Also: cycle prevention including a concurrent-move test; depth cap behavior; pagination correctness (no duplicates/skips across pages while rows are inserted).

CHECKPOINT: matrix and concurrency tests green. Update PROGRESS.md, commit.
```

---

## PHASE 5: Upload flow + quota + idempotency

```
Do Phase 5 only. Read AGENTS.md and SPEC.md sections 6, 8, 10 (flow 1).

Implement services/storage_service.py (aioboto3): presign PUT/GET, head, ranged get, stream get, delete. Storage key is `{owner_id}/{file_id}`; the user filename is never part of a path.
quota_service: atomic reservation via a single `UPDATE users SET storage_used_bytes = storage_used_bytes + :n WHERE id = :owner AND storage_used_bytes + :n <= storage_quota_bytes RETURNING ...`; release on failure/expiry/delete.
Idempotency: `Idempotency-Key` header on POST /files/init, stored in idempotency_keys with a request hash; same key + same body returns the stored response; same key + different body -> 422.
POST /files/init: actor must be editor+ on the folder; max 25 MB; reserve quota; create files row in `pending_upload` with upload_expires_at = now+15m; return presigned PUT.
POST /files/{id}/complete: HEAD the object, size must equal declared else delete object + release quota + 409; ranged-GET first bytes and sniff the real type via magic bytes against the allowlist (jpeg, png, webp, gif, pdf; else kind=other; reject svg/html); then in ONE transaction set status `uploaded` and insert the `file.uploaded` outbox row (include trace context in payload). Return 202. Do NOT enqueue to Celery here (that's the relay's job in Phase 6).
Editor uploads are charged to the folder OWNER's quota.
GET /files/{id} returns status + a 5-minute presigned GET; Content-Disposition attachment for non-images with a sanitized filename.

Tests against MinIO: happy path; quota exceeded; size mismatch; wrong magic type; double-complete is a no-op; idempotent init replay; viewer cannot upload (404/…); editor upload charges owner.

CHECKPOINT: end-to-end upload test green. Update PROGRESS.md, commit.
```

---

## PHASE 6: Outbox + relay + Celery + sweeper

```
Do Phase 6 only. Read AGENTS.md and SPEC.md section 6 (outbox, jobs, workers) closely.

- workers/celery_app.py: Celery with Redis broker, acks_late=True, reject_on_worker_lost=True, worker_prefetch_multiplier=1, soft/hard time limits, visibility_timeout larger than the longest task. Concurrency 1 for the ML worker.
- workers/relay.py: loop that claims unpublished outbox rows with SELECT ... FOR UPDATE SKIP LOCKED in small batches, sends to Celery, sets published_at, increments attempts. Safe to run multiple relay instances.
- workers/tasks.py: process_file(file_id) that ONLY drives the state machine for now (conditional UPDATE uploaded->processing->done using `UPDATE ... WHERE status IN (...)` so duplicate deliveries are no-ops). Each task body runs via asyncio.run(...) with an engine created inside the task using NullPool, reusing the same async repositories. Retry with exponential backoff + jitter, max retries, a NonRetryableError class that goes straight to `failed` with an error message.
- workers/sweepers.py (Celery beat or a periodic task): re-enqueue files stuck in uploaded/processing past a timeout, expire pending_upload rows (delete the object if it exists, release quota), nothing else yet.

Tests (tests/failure_injection/): kill the worker mid-task then restart -> file ends `done`; deliver the same outbox event twice -> processed once; kill the relay between send and mark-published -> duplicate delivery tolerated; two relays running -> no row published by both in the same cycle; sweeper rescues a stuck file.

CHECKPOINT: all failure-injection tests green. Update PROGRESS.md, commit.
```

---

## PHASE 7: Scan + image + PDF pipelines

```
Do Phase 7 only. Read AGENTS.md and SPEC.md section 10 (flow 3).

Extend process_file with the pipelines:
1. Stream the object to a temp file while computing SHA-256; mismatch with the declared hash -> non-retryable reject/quarantine.
2. pipelines/scan.py: ClamAV via clamd INSTREAM. Infected -> status `quarantined`, enqueue a `storage.delete` outbox event, release quota. If clamd is unreachable, retry (don't mark clean).
3. pipelines/image_pipeline.py: read dimensions with Pillow BEFORE decoding (max pixels guard), then CLIP zero-shot labels above a confidence threshold; replace this file's file_labels in one transaction.
4. pipelines/pdf_pipeline.py: PyMuPDF text extraction (cap 200k chars), topic via embedding-based zero-shot among work/study/finance/id/receipt/other; store doc_topic, extracted_text, search_vector, and a doc_topic label.
5. kind=other: scan only.
6. pipelines/models_registry.py loads models ONCE per worker process (worker_process_init). ML_STUB=1 swaps in a fixed-delay fake returning deterministic labels. Verify the real model APIs against the installed library versions; don't guess.

Tests: EICAR test file is quarantined and quota released; decompression-bomb image rejected cheaply; corrupt PDF -> `failed` without retry loops; stub-mode pipeline end-to-end; reprocessing the same file is idempotent (no duplicate labels).

CHECKPOINT: tests green in stub mode; one manual run with real models on a handful of fixtures, with memory usage noted in PROGRESS.md. Commit.
```

---

## PHASE 8: Sharing (users + links)

```
Do Phase 8 only. Read AGENTS.md and SPEC.md section 9 (sharing, links) closely.

sharing_service + routers:
- Grant/update/revoke a direct permission (viewer|editor) on a file or folder; owner only. Share by email: unknown email -> 404 (document the user-enumeration trade-off in docs/SECURITY.md; rate limit this endpoint).
- GET /shared-with-me: keyset paginated, includes items shared directly (ancestor-inherited items appear under their shared root).
- Public links: create (viewer only; allow_download flag; optional password; optional expiry), list, revoke. Token = secrets.token_urlsafe(32); store only its sha256; show the plaintext once.
- public_links router: GET /s/{token} and /s/{token}/folders/{id}, /s/{token}/files/{id}. Anonymous, view-only. Revoked/expired/wrong password/unknown -> identical 404. A folder link allows only descendants (check via CTE). No caching of link validity: revocation must apply on the next request. Headers: Referrer-Policy: no-referrer. Strict IP+token rate limit. Downloads are 5-minute presigned GETs; attachment disposition for non-images.
- Every create/revoke/grant writes an audit_log row in the same transaction.

Tests: extend the authz matrix with grants and links (inheritance, revoke effect on next request, trashed ancestor, link on folder can't reach siblings or parents, password, expiry, enumeration-resistant 404s); editor can upload into a shared folder and quota goes to the owner.

CHECKPOINT: authz matrix green including sharing. Update PROGRESS.md, commit.
```

---

## PHASE 9: Search

```
Do Phase 9 only. Read AGENTS.md and SPEC.md section 10 (flow 5).

GET /search?q=&kind=&label=&folder_id= combining tsvector full-text and pg_trgm similarity on name, scoped to what the actor can access (owned + directly shared + inside shared folders via ancestors). Rank by ts_rank, keyset-paginated with a stable tie-break. All SQL in repositories.
Verify the search_vector is built from name + extracted_text and updates when the worker fills the text.

Tests: a user can't find a stranger's files through any query; shared-folder contents are searchable by the grantee; revoked share disappears from results; trigram catches typos; pagination stable. Attach EXPLAIN (ANALYZE, BUFFERS) output for the main query on a seeded dataset (~100k files) into docs/adr/0002-search.md, confirming the GIN indexes are used.

CHECKPOINT: tests green and the ADR has real plans. Update PROGRESS.md, commit.
```

---

## PHASE 10: Audit, trash, purge, labels browse

```
Do Phase 10 only. Read AGENTS.md and SPEC.md section 10 (flows 4 and 6).

- audit_service: write rows for login, share/link create/revoke, delete/purge, restore. Append-only; no update/delete code paths. Add a monthly partitioning plan in docs/SCALING.md (don't implement yet).
- Trash: soft delete flags the top-level item only; GET /trash lists top-level trashed items; restore; owner-only permanent delete.
- Permanent delete: in ONE transaction delete rows, release quota, and insert `storage.delete` outbox rows. Folder purge is a batched worker job (`folder.purge`, e.g. 500 rows per transaction), idempotent and resumable. Never delete the storage object inside the request.
- Sweeper also purges trash older than 30 days.
- GET /labels and GET /labels/{id}/files, access-checked and keyset paginated.

Tests: purge a folder with 5k files across a worker restart ends complete with correct quota; deleting never leaves a DB row without its storage cleanup event or an object without a row; audit rows exist for each sensitive action.

CHECKPOINT: tests green. Update PROGRESS.md, commit.
```

---

## PHASE 11: Hardening + observability

```
Do Phase 11 only. Read AGENTS.md and SPEC.md sections 5 and 6.

- Prometheus metrics: request latency histograms by route, in-flight requests, DB pool usage, outbox lag (age of oldest unpublished row), queue depth, job duration/failures by type, quota rejections.
- OpenTelemetry tracing for FastAPI, SQLAlchemy and Celery, propagating trace context through the outbox payload so one trace spans upload -> relay -> worker. docker-compose.observability.yml with Prometheus, Grafana (dashboards committed as JSON), Jaeger or Tempo, postgres-exporter, redis-exporter, cAdvisor. Enable pg_stat_statements.
- Confirm logs never contain tokens, passwords or presigned URLs (add a test that scans captured logs).
- CORS, security headers, request size limits, consistent error shapes.
- docs/SECURITY.md: threat model (IDOR, upload attacks, link guessing, enumeration, SSRF n/a, zip/pixel bombs), and what each control does.
- docs/ARCHITECTURE.md with a diagram (mermaid) of the upload -> outbox -> relay -> worker flow, and docs/WHY.md explaining each major choice in plain language.

CHECKPOINT: one upload shows up as a single trace across API, relay and worker; dashboards render real data. Update PROGRESS.md, commit.
```

---

## PHASE 12: Load test + report

```
Do Phase 12 only. Read AGENTS.md, SPEC.md (section 11 if present, else the testing guidance in the spec) and keep the 16 GB RAM constraint in mind: run ML_STUB=1, and never run Ollama alongside.

Build loadtest/ with:
- seed.py: bulk COPY into Postgres (users, folder trees, files, labels, permissions) with presets S (1k users/10k files), M (10k/100k), L (50k/1M, refuse if disk is insufficient). Print table and index sizes.
- locustfile.py: Browser 80% (login once, browse folders, open files, search, shared-with-me), Uploader 15% (init -> presigned PUT -> complete -> poll to done; unique bytes per upload), Sharer 5% (grant/revoke, link create, anonymous link access).
- Runs as make targets: smoke, load (ramp to 1000 hold 20 min), stress (find the knee: p95 > 2s or errors > 1%), spike, soak (2h), data-size sweep at S/M/L, worker throughput benchmark (real ML, 1 vs 2 vs 4 worker containers), access-check benchmark (recursive CTE at folder depths 1/5/10/20; decide on a closure table only if the data says so, record as an ADR).
- REPORT.md: machine specs at the top; per run config, p50/p95/p99 per endpoint, RPS, error rate, and the FIRST bottleneck with evidence (EXPLAIN ANALYZE, pg_stat_statements, metrics). Then an optimization loop: one fix at a time, before/after table, keep or revert. End with a capacity statement and a scaling plan for 10k/100k/1M users.

Rules: never load test shared or production deployments; every claim must point to a metric or query plan.

CHECKPOINT: REPORT.md has real numbers for smoke/load/stress and at least one data-driven optimization with before/after. Commit.
```

---

## PHASE 13: CI/CD + deploy

```
Do Phase 13 only. Read AGENTS.md.

- .github/workflows/ci.yml: ruff, mypy, pytest (with service containers), docker build for api and worker targets.
- deploy.yml: build and push images, run `alembic upgrade head` as a pre-deploy step (migrations must be backward compatible: expand/contract), deploy to a cheap VPS behind Caddy or Traefik with automatic TLS. Secrets only via CI/host env, never in the repo.
- Production compose: api (2 replicas), worker, relay, postgres with volume + backup script (pg_dump to object storage on a schedule, plus a documented restore test), redis, clamav, reverse proxy. Resource limits set.
- README: what it is, architecture diagram, how to run locally, how to run tests, how to deploy, link to REPORT.md and docs/.
- docs/SCALING.md: what breaks first at 10k / 100k / 1M users and the planned fix at each step, tied to the load-test findings.

CHECKPOINT: a push to main deploys; /readyz is green on the public URL; a restore from backup was actually tested once. Update PROGRESS.md, commit.
```

---

## Tips if the local model struggles

- If it loses the thread, start a new session and say: "Read AGENTS.md and PROGRESS.md, then continue with Phase N from its prompt below" and paste only that phase.
- If a phase is too big for the model, ask it to split the phase into 3-4 sub-steps first and do them one by one, committing each.
- After each phase, ask it: "List every deviation from docs/SPEC.md you made in this phase and why." Anything unexpected is a bug or a spec gap.
