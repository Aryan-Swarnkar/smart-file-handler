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

## Phases

| Phase | Scope | Status |
|---|---|---|
| P0 | Skeleton: config, logging, errors, app factory, health, compose (pgvector-pg16, redis, SeaweedFS S3, api), Dockerfile | ✅ done (verified 2026-11-12) |
| P1 | DB + Alembic async: models, initial migration + vector extension | pending |
| P2 | Auth: register/login/refresh rotation/logout, rate limiting | pending |
| P3 | Upload flow: presigned init/complete, dedup, validation, keyset pagination | pending |
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

- `/readyz` checks redis only → DB check lands in P1, S3 (SeaweedFS) check in P3.
- Dockerfile `worker` target exists but inert (no `app.workers` until P4).
- `/metrics` exposes default process metrics only; RED HTTP metrics land in P8.
- README still stage-1 placeholder; rewritten in P9 with architecture/scaling docs.
- ML deps (insightface, open_clip, torch, PyMuPDF, office parsers) added in P5/P6.
