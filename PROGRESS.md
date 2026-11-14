# PROGRESS — v2 → v3 migration

Source of truth: `docs/SPEC.md` · Audit: `MIGRATION_PLAN.md` · v2 history archived at
`docs/PROGRESS_V2.md`.

Conventions: ONE phase at a time; stop at every checkpoint; tests run at least TWICE
before a phase is declared done; one commit per phase; destructive actions (CUT removals,
DB/storage wipes) are shown before execution and need an explicit OK; `web/` is frozen
during phases 1–10 — breaking API changes are logged under the phase below so the
post-Phase-10 UI pass can consume them.

## Phase checklist

- [ ] PROMPT 0 — bootstrap + audit (AGENTS.md, MIGRATION_PLAN.md, this checklist)
- [ ] Phase 1 — cleanup + skeleton (CUT list shown + OK'd before any deletion; MinIO availability check)
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
