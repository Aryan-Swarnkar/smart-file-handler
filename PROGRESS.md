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
- [x] Phase 2 — schema + migrations (Path A dev-DB/storage wipe — confirmed + executed 2026-11-15)
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
| 2026-11-15 | Phase 2 | (this commit) | Path A wipe executed after explicit OK: 768 users / 2,330 file rows dropped (pg_dump kept in gitignored `backups/`), 2,299 SeaweedFS objects deleted (bucket emptied, `s3_data` volume kept), 2 alembic revisions git-removed, postgres image → `postgres:16-alpine` (pg_trgm + citext are contrib). Logged-in session preserved: bruhmoment account row + its 1 live refresh token re-inserted post-migration (token valid to Nov 21 — no forced logout). New v3 schema: 11 tables (users w/ citext email + quota columns, refresh_tokens, folders, files, labels, file_labels, permissions, share_links, audit_log, outbox, idempotency_keys) with XOR CHECKs on permissions/share_links, explicit ON DELETE everywhere, partial indexes (live rows / unpublished outbox), generated `search_vector` tsvector + GIN, pg_trgm GIN on name, 4 native enums. Kind enum shrunk to {image, pdf, other} per Phase 5 allowlist; status gains `quarantined`; `extracted_text_preview` → `extracted_text`. Registration sets quota from `DEFAULT_STORAGE_QUOTA_GB`. Hand-written initial migration `20261115_v3_initial`; dance upgrade→downgrade base→upgrade passed; `alembic check` no drift. docs/adr/0001 (adjacency list + CTE, depth cap 20). **Breaking API (frozen UI):** none — response shapes unchanged; note `kind` never returns `document`/`text` anymore (uploads of those now report `other`). Evidence: ruff + mypy clean (77 files); pytest 59 passed ×2 consecutive full runs; new tests/test_schema_constraints.py (15 DB-enforcement tests incl. generated tsvector + cascade). Post-checkpoint verification pass (all 4 once-only gaps closed): 2nd migration dance clean + `alembic check` no drift; pg_dump restore-proven into a scratch DB (exact 768/2330/594 counts, CUT pgvector content correctly excluded, scratch dropped); live container E2E on :8000 11/11 (register→login→init→presigned PUT→complete→list→GET round-trip→delete→404 — the ASGI-tests-can't-reach path); degraded /readyz re-verified on rebuilt stack (redis stop → 503 → start → 200). |
