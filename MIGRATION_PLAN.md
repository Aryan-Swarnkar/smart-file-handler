# MIGRATION_PLAN.md — v2 → v3 audit (PROMPT 0, no code changed)

Audit date: 2026-11-14 · HEAD at audit: `204b26c` · Source of truth: `docs/SPEC.md`

The spec copy in the repo contains PROMPT 0 + the 13 phase prompts only; the master doc
(§1–16) is missing. Per the user's answers below, we proceed phase-by-phase from the
prompts and list every inferred decision for sign-off instead of guessing.

## 1. Decisions taken (user answers + recommendations)

| # | Question | Decision |
|---|---|---|
| Q1 | Real data in dev DB — regenerate initial migration or expand/contract? | **Path A**: dev data is disposable → wipe dev DB + object storage at Phase 2 and regenerate ONE v3 initial migration. The wipe itself is a Phase-2 action and is confirmed again before it runs. |
| Q2 | Master doc missing — wait or proceed? | **Prompts-only**: no master doc is coming; every inferred decision is listed per phase for sign-off. |
| Q3 | What happens to `web/`? | **Recommendation (pending veto): freeze** `web/` during phases 1–10, note breaking API changes per phase in PROGRESS.md, one UI alignment pass after Phase 10. |
| Q4 | Object storage | **MinIO (draft)**, backend-agnostic env config retained (`S3_ENDPOINT_URL` / `S3_PUBLIC_ENDPOINT_URL` via aioboto3). Spec names MinIO; it honors `response-content-disposition` (SeaweedFS ignored it in v2). **Caveat to verify in Phase 1**: v2 compose comments claim the MinIO community edition was discontinued — verify image availability first; fallback = keep SeaweedFS, env-only (documented spec deviation; user-approved precedent exists). |

Other standing decisions:

- Archive plan: `git mv PROGRESS.md docs/PROGRESS_V2.md` (v2 history preserved); a fresh
  PROGRESS.md checklist replaces it.
- Free & legal rule: open-source only (MIT/Apache/BSD preferred; AGPL only for
  separately-run containers, flagged); record licenses; no paid SaaS.
- Phase 2 wipe scope: dev Postgres :5433 (all data). The `s3_data` docker volume is left
  inert on disk unless the user says delete.
- Deferred to Phase 7: clamd client choice (hand-rolled INSTREAM vs library —
  license-checked then) and open_clip checkpoint-license verification.

## 2. Migration risk & data strategy (spec-mandated question, answered)

- Real data EXISTS in dev: ~1,926 `done` file rows on postgres :5433 (the user's real
  uploaded college files) + ui-tester artifacts; objects live in the `s3_data` volume
  (SeaweedFS).
- Alembic currently has 2 revisions (`20261112_bbecbe6aa8ec` pgvector initial,
  `20261113_a3f1c2d4e5f6` dedup nullable sha256). Both die under Path A.
- Chosen: **Path A** (Q1). At Phase 2: user confirms → drop/recreate the dev DB, wipe
  object storage, remove the 2 alembic revisions, generate ONE v3 initial migration,
  verify `upgrade head` / `downgrade base` / `upgrade head` + `alembic check`.
- Expand/contract was the alternative and is explicitly NOT chosen (no production data).

## 3. What exists and is reusable as-is (or near)

| File | Status |
|---|---|
| `app/db/base.py` | KEEP — naming conventions + CreatedAtMixin already match v3 requirements. |
| `app/db/models/label.py` | NEAR-REUSE — labels + file_labels exist; adjust FK targets to the v3 schema. |
| `app/db/session.py` | KEEP + small change — ensure `expire_on_commit=False` and the `get_session` dependency. |
| `app/core/config.py` | FOUNDATION good — pydantic-settings 12-factor with secret validation; contents change heavily (see §5). |
| `app/core/logging.py` | KEEP — structlog JSON; add request-ID middleware if absent (Phase 1). |
| `app/core/errors.py` | KEEP — domain exceptions + handlers; align to 404-on-no-access semantics. |
| `app/services/security.py` | KEEP — argon2 + JWT helpers (deps already present: argon2-cffi, pyjwt). |
| `app/services/sniffing.py` | REUSE — magic-byte sniffing for the Phase 5 allowlist (jpeg/png/webp/gif/pdf). |
| `app/services/storage.py` | EXTEND — aioboto3 client exists; add presign PUT/GET, head, ranged get, delete; keep the endpoint / public-endpoint split. |
| `app/api/routers/health.py` | EXTEND — /healthz exists; add /readyz with real db / redis / storage probes. |
| Alembic setup | KEEP infra — async env.py per Phase 2; the revisions themselves are wiped (Path A). |
| tests/ | KEEP structure — reorganize into tests/authz_matrix/ and tests/failure_injection/. |
| Dockerfile / docker-compose.yml | KEEP infra — reshape (see §5). |
| `/metrics` route (prometheus-client) | KEEP — extended in Phase 11. |

## 4. What exists but is CUT (Phase 1 re-presents this list + waits for OK)

| Item | Where | Note |
|---|---|---|
| ARQ | `arq` dep in pyproject; `app/workers/settings.py`; `app/workers/pipeline.py`; ARQ pool in `app/main.py` lifespan | Replaced by Celery + relay (Phase 6). API keeps no queue connection. |
| Faces/persons + pgvector | `pgvector` dep; `app/db/models/face.py`; `app/db/models/person.py` (Vector(512) + HNSW); model imports | Faces feature removed from the product. |
| Sentry | `SENTRY_DSN` in `app/core/config.py` + `.env.example`; compose plumbing | sentry-sdk was never actually installed — the hook is dead config. |
| Per-user dedup-by-reuse | sha256 usage in `app/db/models/file.py` + any dedup logic in `app/services/files.py` / repositories | The sha256 column STAYS as an upload-integrity check (Phase 7 compares declared vs computed). |
| SSE | — | Not present in the v2 tree; nothing to remove (grep-verify in Phase 1). |
| Admin code | — | None found in the v2 tree; grep again in Phase 1 to confirm. |

## 5. What exists but must change (file by file)

| File | Change |
|---|---|
| `app/db/models/user.py` | Add quota fields (`storage_used_bytes`, `storage_quota_bytes`, set at registration). |
| `app/db/models/refresh_token.py` | Add `family_id` + rotation / reuse-detection support. |
| `app/db/models/file.py` | Reshape: `folder_id` FK; status enum (`pending_upload`/`uploaded`/`processing`/`done`/`quarantined`/`failed`); `upload_expires_at`; `doc_topic`; `extracted_text`; generated `search_vector` (tsvector); keep `sha256` (integrity only); name columns for search + quota accounting. |
| `app/db/models/__init__.py` | Drop face/person imports; add v3 models as they land. |
| `app/services/rate_limit.py` | REWRITE — v2 fixed-window → Redis Lua token bucket (atomic), keyed IP+email, 429 + Retry-After (Phase 3). |
| `app/services/auth.py` | Register creates user + root folder in ONE transaction; refresh rotation + family revocation on reuse. |
| `app/services/files.py` | Rewrite to the v3 flow: `POST /files/init` (editor+ on folder, 25 MB cap, quota reserve, `pending_upload` row + expiry, presigned PUT) and `complete` (HEAD, size equality, magic-byte sniff, status + outbox row in ONE tx). |
| `app/api/routers/files.py` | init/complete endpoints; GET file returns a 5-min presigned GET; attachment disposition for non-images with a sanitized filename. |
| `app/api/deps.py` | `get_current_user` dependency per v3 (15-min access JWT). |
| `app/main.py` | Remove the ARQ pool from lifespan; keep the app-factory pattern; wire /readyz. |
| `app/workers/` | `settings.py` + `pipeline.py` deleted (ARQ); replaced by `celery_app.py`, `relay.py`, `tasks.py`, `sweepers.py`, `pipelines/` (Phases 6–7). |
| `app/schemas/` | auth.py / files.py reshaped to v3 request/response shapes. |
| `pyproject.toml` | Remove `arq`, `pgvector`; add `celery[redis]`, `Pillow`, `pymupdf`, (Phase 11) `opentelemetry-*`, (Phase 12, dev) `locust`; update the description line (it still advertises faces); ruff/mypy config extended to new dirs. |
| `.env.example` | Remove SENTRY_DSN + ARQ knobs; add MinIO/S3 vars, `CLAMAV_*`, `ML_STUB`, quota knobs. |
| `docker-compose.yml` | SeaweedFS service → MinIO (pending the Q4 availability check); add `worker`, `relay` (placeholder cmd), `clamav`; healthchecks + memory limits (16 GB host). |
| `Dockerfile` | Multi-stage with `api` and `worker` targets (Phase 1). |

## 6. What is missing (built in later phases)

Models: folders, permissions, share_links, outbox, idempotency_keys, audit_log.
Services/routers: AccessService, folder service, sharing service (+ public links),
quota service, search, labels browse, trash/restore/purge.
Workers: Celery app, relay, tasks, sweepers, scan/image/PDF pipelines.
Infra/docs: /readyz; OpenTelemetry + Prometheus dashboards (Phase 11); loadtest/ +
REPORT.md (Phase 12); CI/CD + deploy + backup/restore (Phase 13);
docs/adr/0001, docs/adr/0002, docs/SECURITY.md, docs/ARCHITECTURE.md, docs/WHY.md,
docs/SCALING.md.

## 7. Dependency ledger (free & legal rule)

Current deps carry over unless listed as CUT in §4. Licenses marked ✱ are to verify
during the Phase 1 dependency sweep (cheap: read installed package metadata).

| Dependency | Status | License |
|---|---|---|
| fastapi / pydantic / pydantic-settings | keep | MIT |
| uvicorn[standard] | keep | BSD-3 |
| structlog | keep | MIT/Apache ✱ |
| prometheus-client | keep | Apache-2.0 ✱ |
| sqlalchemy[asyncio] / greenlet | keep | MIT |
| asyncpg | keep | Apache-2.0 ✱ |
| alembic | keep | MIT |
| redis | keep (Phase 6: bumped `>=6.1` — kombu 5.7's redis transport enforces it at runtime, VersionMismatch otherwise; rate-limit usage API-compatible; resolved 8.1.0) | MIT |
| aioboto3 (+ types-aioboto3) | keep | Apache-2.0 ✱ |
| argon2-cffi | keep | MIT ✱ |
| pyjwt | keep | MIT ✱ |
| email-validator | keep | MIT ✱ |
| **arq** | **CUT (Phase 1)** | — |
| **pgvector** | **CUT (Phase 1)** | — |
| **celery** | ADD (Phase 6) | BSD-3-Clause |
| **packaging** | ADD (Phase 6) — celery 5.7.0 imports it but declares it nowhere; pinned explicitly | Apache-2.0 / BSD-2-Clause |
| **Pillow** | ADD (Phase 7) | HPND (permissive) ✱ |
| **pymupdf** | ADD (Phase 7) | **AGPL-3.0** — acceptable per the user rule (separately-run worker container); flagged |
| **open_clip** | ADD (Phase 7) | MIT (code); **checkpoint weights' license verified at Phase 7 before any real-model run** |
| **opentelemetry-\*** | ADD (Phase 11) | Apache-2.0 |
| **locust** | ADD (dev, Phase 12) | MIT |

Rule: nothing outside this table without asking first; each new dependency gets its
license recorded here when it is introduced.

## 8. Phase list (adjusted to findings)

| Phase | Scope | Adjustment notes |
|---|---|---|
| PROMPT 0 | bootstrap + audit | This file set; no code changed. |
| 1 | cleanup + skeleton | CUT list re-presented before any deletion; the §7 folder layout is inferred from prompt text and listed for sign-off (master doc missing); MinIO image-availability check happens here. |
| 2 | schema + migrations | Path A wipe (confirm again before executing); ONE initial migration; pg_trgm (+ citext if used) enabled; ADR 0001. |
| 3 | auth + rate limiting | Reuse security.py; rewrite the rate limiter as a Lua token bucket. |
| 4 | folders + AccessService | New; recursive CTE depth cap 20; advisory-lock move; authz matrix tests. |
| 5 | upload + quota + idempotency | Reuse sniffing.py + storage.py; NO Celery enqueue (the relay's job). |
| 6 | outbox + relay + Celery + sweeper | Replaces ARQ entirely. |
| 7 | scan + image + PDF pipelines | ML_STUB default + one manual real-model run; clamd-client and checkpoint-license decisions land here. |
| 8 | sharing (users + links) | New; audit rows written in the same transaction. |
| 9 | search | ADR 0002 with real EXPLAIN (ANALYZE, BUFFERS) plans. |
| 10 | audit, trash, purge, labels | Sweeper extension; monthly partitioning is a docs plan only. |
| 11 | hardening + observability | docker-compose.observability.yml; log-leak scan test. |
| 12 | load test + report | §11 not present in spec → fall back to the prompt's own testing guidance (decided then). ML_STUB=1; never with Ollama co-running. |
| 13 | CI/CD + deploy | From here on migrations must be expand/contract compatible. |

## 9. Ambiguity register (living — never silently resolved)

1. Master doc §1–16 missing (the spec itself notes the original paste was cut off
   mid-§10). Q2 answered: prompts-only; per-phase sign-off of inferred decisions.
2. "no dual writes/outbox" phrasing → interpreted as "the outbox + relay is the ONLY
   path to Celery; the API never enqueues directly" (supported by Phase 5's explicit
   "Do NOT enqueue to Celery here").
3. SeaweedFS → MinIO: draft decision MinIO, pending image-availability verification
   (Phase 1); documented fallback = SeaweedFS env-only.
4. Thumbnails: not in any phase prompt → STRETCH, deferred pending the master doc.
5. web/: unaddressed by the spec → UI-freeze recommendation (Q3), pending veto.
6. SSE / admin code: listed as CUT by PROMPT 0 but absent from the v2 tree →
   grep-verify in Phase 1, then mark "nothing to remove".
7. The §7 folder layout is unknown → Phase 1 proposes the layout explicitly before
   creating files.
8. Phase 12 references §11 "if present" → not present; fallback decided at Phase 12.
9. CLIP checkpoint weights licensing (open_clip code is MIT; weights vary) → verified
   at Phase 7 before real-model runs.
10. clamd client: hand-rolled INSTREAM client vs a library → decided Phase 7
    (license-checked).
