"""PDF pipeline.

PyMuPDF text extraction (cap 200k chars), topic via embedding-based zero-shot
among work/study/finance/id/receipt/other; stores doc_topic, extracted_text,
search_vector, and a doc_topic label. PyMuPDF is AGPL-3.0 — worker runs as a
separately-built container (license-flagged in MIGRATION_PLAN.md §7). Phase 7.
May depend on: repositories.labels, models_registry.
"""
