# PROGRESS — v2 → v3 migration

Source of truth: `docs/SPEC.md` · Audit: `MIGRATION_PLAN.md` · v2 history archived at
`docs/PROGRESS_V2.md`.

Conventions: ONE phase at a time; stop at every checkpoint; tests run at least TWICE
before a phase is declared done; one commit per phase; destructive actions (CUT removals,
DB/storage wipes) are shown before execution and need an explicit OK; `web/` is frozen
during phases 1–10 — breaking API changes are logged under the phase below so the
post-Phase-10 UI pass can consume them.

## Phase checklist

- [x] PROMPT 0 — bootstrap + audit (AGENTS.md, MIGRATION_PLAN.md, this checklist)
- [x] Phase 1 — cleanup + skeleton (CUT list shown + OK'd before any deletion; MinIO availability check)
- [ ] Phase 2 — schema + migrations (Path A dev-DB/storage wipe — confirm before executing)
- [ ] Phase 3 — auth + rate limiting (Lua token bucket)
- [ ] Phase 4 — folders + AccessService + authz matrix tests
- [ ] Phase 5 — upload flow + quota + idempotency
- [ ] Phase 6 — outbox + relay + Celery + sweeper
- [ ] Phase 7 — scan + image + PDF pipelines (ML_STUB default; one manual real-model run)
- [ ] Phase 8 — sharing (users + links)
- [ ] Phase 9 — search (+ ADR 0002 with real EXPLAIN plans)
- [ ] Phase 10 — audit, trash, purge, labels browse (UI freeze ends)
- [ ] Phase 11 — hardening + observability
- [ ] Phase 12 — load test + report (ML_STUB=1; never with Ollama co-running)
- [ ] Phase 13 — CI/CD + deploy

## Log

| Date | Phase | Commit | Notes / breaking API changes for the frozen UI |
|---|---|---|---|
| 2026-11-14 | PROMPT 0 | (this commit) | AGENTS.md + MIGRATION_PLAN.md + fresh checklist created; v2 progress archived to docs/PROGRESS_V2.md. No code changed. |
| 2026-11-14 | Phase 1 | (this commit) | CUT removed: ARQ (dep + worker + API pool), face/person models + pgvector dep, per-user dedup-by-reuse (sha256 kept as integrity only), SENTRY_DSN hook; SSE + admin verified absent (grep). Services renamed `*_service.py` (storage, files). 28 docstring-only stubs for the v3 layout. Compose: +clamav (healthy, clamdcheck.sh verified) +relay/worker placeholders, api healthcheck, memory limits; MinIO confirmed discontinued (Docker Hub 404, dl.min.io 410) → SeaweedFS stays (documented deviation). uv link-mode=copy (OneDrive hardlink breakage) + UV_HTTP_TIMEOUT=120 in Docker builder. **Breaking API (frozen UI):** `deduplicated` field removed from `POST /files/{id}/complete`; processable uploads now stay `status=uploaded` until the Phase 6 worker. pgvector postgres image kept until the Phase 2 wipe (dev volume still has vector columns). Evidence: ruff + mypy clean; pytest 44 passed ×3 runs; live `/readyz` degradation verified (redis stop → 503, restart → 200). |
