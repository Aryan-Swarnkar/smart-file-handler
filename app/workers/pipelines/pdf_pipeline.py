"""PDF pipeline: text extraction (capped) -> topic -> one-tx persistence.

- `pymupdf.open` on garbage raises FileDataError/EmptyFileError -> a
  deterministic "format:" NonRetryableError (retrying cannot succeed);
  password-protected PDFs fail the same way (needs_pass, no cracking).
- Text extraction stops once PDF_TEXT_CHAR_CAP is reached — the cap keeps
  the GENERATED tsvector under PostgreSQL's 1 MB tsvector limit and the row
  small. `search_vector` recomputes in the SAME row write (generated column).
- Topic comes from the registry (stub: deterministic; real: CLIP text
  encoder zero-shot over the six doc topics), and is persisted twice: the
  denormalized files.doc_topic (cheap "by topic" filters) and a per-user
  DOC_TOPIC label — canonical value lives in labels/file_labels.

PyMuPDF is AGPL-3.0 — it runs ONLY in the separately-built worker container
(license-flagged in MIGRATION_PLAN.md §7). Imports `pymupdf` (the 2.x
canonical module; the `fitz` alias is deprecated).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pymupdf
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import LabelKind
from app.repositories.files import FileRepository
from app.repositories.labels import LabelEntry, LabelRepository
from app.workers.errors import NonRetryableError
from app.workers.pipelines.models_registry import get_registry

logger = get_logger(__name__)


def _extract_text(path: Path) -> str:
    """Sync — the caller runs it in a thread (PyMuPDF parse is CPU work,
    rule 8). Deterministic failures raise NonRetryableError.

    Parses from an IN-MEMORY buffer (stream=), not the file path: a FAILED
    `pymupdf.open(path)` leaks its C file handle until GC, which on Windows
    keeps the temp file locked and breaks the task's TemporaryDirectory
    cleanup (repro-proven). Bytes are ≤ the 25 MB upload cap — fine at
    worker concurrency 1, and one less cross-platform file-handle hazard."""
    try:
        data = path.read_bytes()
        doc = pymupdf.open(stream=data, filetype="pdf")
    except (pymupdf.FileDataError, pymupdf.EmptyFileError) as exc:
        raise NonRetryableError("format: unreadable pdf") from exc
    try:
        if doc.needs_pass:
            raise NonRetryableError("format: password-protected pdf")
        cap = get_settings().PDF_TEXT_CHAR_CAP
        parts: list[str] = []
        total = 0
        for page in doc.pages():  # explicit generator (Document's __iter__
            # is the legacy __getitem__ protocol — invisible to mypy)
            parts.append(page.get_text("text"))
            total += len(parts[-1])
            if total >= cap:
                break
        return "".join(parts)[:cap]
    finally:
        doc.close()


async def run_pdf_pipeline(
    factory: async_sessionmaker,
    *,
    file_id: uuid.UUID,
    user_id: uuid.UUID,
    path: Path,
) -> str:
    """One PDF -> extracted_text + doc_topic + doc_topic label, persisted.
    Returns the topic for logging. Raises NonRetryableError for bad files."""
    # 1. Extraction (thread — PyMuPDF is sync CPU work).
    text = await asyncio.to_thread(_extract_text, path)

    # 2. Topic. Stub: deterministic from the file id. Real: CLIP text
    #    zero-shot; empty text classifies as "other".
    registry = get_registry()
    topic = await registry.topic_document(file_id, text)

    # 3. Persist in ONE transaction: text+topic write, get-or-create label,
    #    replace file_labels (a PDF carries exactly one doc_topic label).
    async with factory() as session:
        await FileRepository(session).set_pdf_text(
            file_id, extracted_text=text, doc_topic=topic.topic
        )
        labels = LabelRepository(session)
        entry = LabelEntry(
            name=topic.topic, kind=LabelKind.DOC_TOPIC, confidence=topic.confidence
        )
        resolved = await labels.get_or_create(user_id=user_id, entries=[entry])
        await labels.replace_file_labels(
            file_id=file_id,
            entries=[(resolved[(topic.topic, LabelKind.DOC_TOPIC)], topic.confidence)],
        )
        await session.commit()

    logger.info(
        "pdf_pipeline_written",
        file_id=str(file_id),
        topic=topic.topic,
        text_chars=len(text),
    )
    return topic.topic
