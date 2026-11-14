# AGENTS.md — operating manual for AI sessions on smart-file-handler

Full details in `docs/SPEC.md` (the single source of truth). The repo copy of the spec
contains PROMPT 0 + the 13 phase prompts; the master doc (§1–16) is not present — when a
phase needs a detail the prompts don't give, list the ambiguity and ask, never guess.

## Project (one paragraph)

smart-file-handler is an async FastAPI backend that stores uploads in S3-compatible object
storage and organizes them. We are migrating v2 (flat uploads, ARQ, face/person ML via
pgvector, per-user dedup) to v3 "Drive-style + sharing": folders with inherited
permissions via AccessService, an upload flow with quota + idempotency, outbox → relay →
Celery state-machine pipelines (ClamAV scan, image CLIP labels, PDF text + topic), search
(tsvector + pg_trgm), sharing for users + public links, audit/trash/purge, observability,
load testing, CI/CD. The `web/` frontend is frozen during backend phases.

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2 async + asyncpg · Alembic (async) · PostgreSQL 16
(pg_trgm + generated tsvector; pgvector is CUT) · Redis · Celery (Redis broker) + relay ·
MinIO via aioboto3 · ClamAV (clamd INSTREAM) · Pillow · PyMuPDF (AGPL, separate container)
· structlog · pytest / ruff / mypy · Docker Compose · uv for dependency management.

## Architecture rules

1. Layering: routers → services → repositories → db models. Routers never touch SQLAlchemy or SQL directly.
2. Repositories-only-build-queries: every SQL statement lives in `app/repositories/`; services orchestrate, they don't query.
3. Authz via AccessService: every service method takes an explicit `actor` and calls `AccessService.resolve_role(actor, resource)` first. No access → raise NotFound (404), never 403.
4. No dual writes: the `outbox` table is the ONLY bridge from DB state to Celery. The API never enqueues to Celery directly; the relay is the single publisher (interpretation flagged in MIGRATION_PLAN.md §9).
5. Idempotent state-machine jobs: transitions use conditional `UPDATE ... WHERE status IN (...)` so duplicate deliveries are no-ops.
6. Keyset pagination everywhere; OFFSET is not allowed.
7. Stateless API: no in-process caches of authz decisions or link validity; revocation applies on the next request.
8. No blocking calls inside async code; workers create their own engine (NullPool) inside the task and drive async code via asyncio.run.
9. Config via env only (pydantic-settings, 12-factor); secrets never in the repo.
10. Every FK has an explicit ON DELETE; naming conventions live in `app/db/base.py`.

## CUT — do not reintroduce (from the spec)

ARQ (+ `arq` dep) · face/person models, pipelines and pgvector embeddings · Sentry
(`SENTRY_DSN`; sentry-sdk was never installed) · SSE · per-user dedup-by-reuse (sha256
STAYS, but only as an upload-integrity check) · admin code (none found in v2; grep before
each phase) · direct Celery enqueue from the API.

## STRETCH — do not build unless the user asks

Thumbnails (explicitly deferred). The list is incomplete because the master doc is
missing; new items are added only on explicit user ask.

## Workflow rules (hard)

- ONE phase at a time. Stop at the phase checkpoint and wait.
- Run the test suite at least TWICE before declaring a phase done (flake catch).
- Update PROGRESS.md (status, commit, notes) and make ONE git commit per phase.
- Show the list of anything destructive (files deleted, tables dropped, data wiped)
  BEFORE doing it and wait for an explicit OK.
- Never delete data or drop tables without asking first.
- Never invent library APIs: check the installed version's docs/source when unsure.
- Prefer small files; split large phases into sub-steps with separate commits if needed.
- Ask before adding any dependency not in the spec, and record its license
  (MIGRATION_PLAN.md § dependency ledger).
- Be able to justify every line of code; if you can't, rewrite it.

## Free & legal rule

Everything stays free and legally clean: open-source licenses only (MIT / Apache / BSD
preferred; AGPL acceptable only for separately-run components, e.g. PyMuPDF, and must be
flagged); no paid SaaS. Record each new dependency's license when it is introduced.

## ML_STUB convention

`ML_STUB=1` (the default) makes workers use fixed-delay fakes that return deterministic
labels instead of real models. Real models run only on explicit ask, as one manual run,
and NEVER alongside the full stack + Locust on this 16 GB host (no Ollama co-running).

## Environments & live state (do not break)

- `docker compose up`: postgres :5433 · redis :6379 · object storage :9000 · api :8000 ·
  worker; Vite dev server :5173.
- The user's browser tab stays logged in — never force logout / rotation of that
  session's refresh tokens.
- `web/` is FROZEN during phases 1–10; breaking API changes are noted per phase in
  PROGRESS.md; one UI alignment pass happens after Phase 10.

## Checks & testing

- `uv run pytest` (twice), `uv run ruff check .`, `uv run mypy app`.
- Migrations: `alembic upgrade head` → `downgrade base` → `upgrade head`, plus
  `alembic check` for drift.
- EICAR test string is legal and expected for AV-scan testing.

## Commits

One commit per phase, message `Phase N: <one-line summary>` (the PROMPT 0 bootstrap has
its own commit). Commits are local; the user pushes manually.
