"""files — uploaded objects and their processing state."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin
from app.db.models.label import FileLabel

# Canonical expression for the GENERATED search_vector column. Imported by the
# initial migration so the two definitions cannot drift (alembic check
# compares the stored expression byte-for-byte).
SEARCH_VECTOR_EXPR = (
    "to_tsvector('english', coalesce(original_name, '') || ' ' || "
    "coalesce(extracted_text, ''))"
)


class FileStatus(enum.StrEnum):
    # pending_upload: init'd, quota reserved, bytes not yet verified by
    # /complete (Phase 5). uploaded: verified + outbox row written — the
    # Phase 6 worker claims it from here.
    PENDING_UPLOAD = "pending_upload"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    QUARANTINED = "quarantined"  # Phase 7: ClamAV infected


class FileKind(enum.StrEnum):
    # v3 allowlist (Phase 5 sniff): jpeg/png/webp/gif -> image, pdf -> pdf,
    # everything else (incl. txt/docx) -> other. No office/text pipelines exist.
    IMAGE = "image"
    PDF = "pdf"
    OTHER = "other"


file_status_enum = SQLAlchemyEnum(
    FileStatus,
    name="file_status",
    native_enum=True,
    values_callable=lambda e: [m.value for m in e],
)
file_kind_enum = SQLAlchemyEnum(
    FileKind,
    name="file_kind",
    native_enum=True,
    values_callable=lambda e: [m.value for m in e],
)


class File(Base, CreatedAtMixin):
    __tablename__ = "files"
    __table_args__ = (
        # Keyset pagination on the root listing (and, later, trash view):
        # non-partial so trashed rows stay seekable in Phase 10.
        Index("ix_files_user_created_id", "user_id", "created_at", "id"),
        # Folder browse: children of one folder, live rows only (partial).
        Index(
            "ix_files_user_folder_created",
            "user_id",
            "folder_id",
            "created_at",
            "id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Full-text (Phase 9): GIN over the generated tsvector.
        Index("ix_files_search_vector", "search_vector", postgresql_using="gin"),
        # Fuzzy name matching (Phase 9): trigram GIN over the original name.
        Index(
            "ix_files_name_trgm",
            "original_name",
            postgresql_using="gin",
            postgresql_ops={"original_name": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # NULL = the user's root ("My Files"). CASCADE: purging a folder purges
    # its files (Phase 10 batches the storage deletes via outbox).
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"),
        nullable=True,
    )
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    # BigInteger: Integer overflows at 2 GiB (PG int4).
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # NULL until /complete hashes the real bytes (client-supplied hashes are
    # never trusted). Integrity check only — v3 has no dedup-by-reuse.
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)

    status: Mapped[FileStatus] = mapped_column(
        file_status_enum, nullable=False, default=FileStatus.UPLOADED
    )
    kind: Mapped[FileKind | None] = mapped_column(
        file_kind_enum, nullable=True
    )  # set at /complete by magic-byte sniff
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Denormalized topic for cheap "files by topic" filters without a join;
    # canonical value also lives in labels/file_labels (Phase 7 writes both).
    doc_topic: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Full extracted text (Phase 7 pdf pipeline, cap 200k chars enforced
    # there — keeps the generated tsvector under PG's 1 MB tsvector limit).
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # trash (Phase 10); NULL = live row

    # Phase 5: set at init (now + UPLOAD_TTL); a pending row past this stamp
    # is abandoned — the Phase 6 sweeper deletes it and releases the quota.
    upload_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # GENERATED ALWAYS ... STORED: maintained by the DB when name/text change,
    # so Phase 9 search reads it and Phase 7's text write updates it for free.
    # Read-only from the ORM's perspective — never assign it.
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(SEARCH_VECTOR_EXPR, persisted=True),
        nullable=True,
    )

    # passive_deletes: file_labels.file_id is a composite-PK member — the
    # ORM's default parent-delete behavior (SET NULL the child FK) is
    # ILLEGAL here (AssertionError at flush, found by the live E2E's first
    # delete of a labeled file). The FK's DB-level ON DELETE CASCADE removes
    # the association rows instead; the per-user Label rows survive.
    labels: Mapped[list[FileLabel]] = relationship(
        back_populates="file", passive_deletes=True
    )
