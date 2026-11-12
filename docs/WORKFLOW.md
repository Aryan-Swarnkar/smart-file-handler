# WORKFLOW — smart-file-handler

**One living document. Updated at the end of every phase.**
Describes the exact runtime workflow: what happens when you click/call each
endpoint, which function runs, which file it lives in, what algorithm and
technology it uses, and where each file sits in the chain.

Reading order convention:
`file/function()` — file path relative to repo root.

---

## System map (current: after Phase 0)

```
Browser/curl ──HTTP──▶ API (uvicorn, FastAPI) ──▶ structlog JSON logs
                        │            │
                        │            ├─▶ Redis (readiness ping)
                        │            └─▶ PostgreSQL 16+pgvector (unused yet, P1)
                        │            └─▶ SeaweedFS S3 API (unused yet, P3)
                        └─▶ /metrics (prometheus-client)
```

## File responsibility map (current: after Phase 0)

| File | Owns | Used by |
|---|---|---|
| `app/core/config.py` | `Settings` class + `get_settings()`; all env parsing; production secret validation | everything (never imports other app modules) |
| `app/core/logging.py` | `configure_logging()` (structlog JSON/console), `RequestIdMiddleware` (pure ASGI), `get_logger()`, `request_id_var` contextvar | main, errors |
| `app/core/errors.py` | Domain exception classes + `install_error_handlers()` → one JSON envelope | main (wires once) |
| `app/api/routers/health.py` | `/healthz`, `/readyz`, `/` index routes | main |
| `app/main.py` | `create_app()` factory + module `app`; lifespan (redis pool), middleware order, router registration, `/metrics` route (explicit, no redirect) | uvicorn, tests |
| `tests/conftest.py` | env bootstrapping before app import, `client` fixture (lifespan-aware) | all tests |
| `tests/test_smoke.py` | P0 endpoint contract tests | pytest |
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
4. Scope: redis only at P0. PostgreSQL check lands P1, storage check P3.

## Workflow 4 — Any error response (envelope)

1. Service code raises e.g. `NotFoundError` (`app/core/errors.py`).
2. Exception bubbles to the registered handler → `JSONResponse` with
   `error.code = "not_found"`, HTTP 404.
3. Unhandled `Exception` → handler logs full traceback via
   `logger.exception()` (structlog) but returns generic
   `{"error":{"code":"internal_error","message":"Internal server error."}}` —
   internals never leak to clients.

## Workflow 5 — Test run (`uv run pytest`)

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

## Workflow 6 — Docker image build (Dockerfile)

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

## Workflow 7 — `docker compose up` (local stack)

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

## Technologies in play at P0

- **FastAPI** (ASGI) + **uvicorn** — HTTP layer, fully async handlers.
- **pydantic-settings v2** — `Settings`; env vars > `.env` > code defaults;
  `@model_validator(mode="after")` INSIDE the class enforces production
  secret strength (this fixes stage-1's broken module-level validator).
- **structlog** — JSON structured logs, contextvars-based request IDs.
- **prometheus-client** — `/metrics` (default process metrics at P0).
- **redis-py asyncio** — readiness ping (pool reused for rate limiting P2,
  ARQ broker P4).
- **httpx + pytest-asyncio** — in-process ASGI integration tests.
- **uv** — locked, cached dependency installs (host + Docker builder).
- **Docker Compose** — 4-service local stack with health-gated startup.
