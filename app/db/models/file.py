"""files — uploaded objects and their processing state."""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin
from app.db.models.label import FileLabel


class FileStatus(enum.StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class FileKind(enum.StrEnum):
    IMAGE = "image"  # jpg/jpeg/png/webp -> CLIP zero-shot labels (Phase 7)
    PDF = "pdf"  # PyMuPDF extraction (Phase 7)
    DOCUMENT = "document"  # docx/pptx/xlsx -> office text extraction
    TEXT = "text"  # txt/md/csv -> direct read
    OTHER = "other"  # unprocessable binary: stored, doc_topic="other"


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
        # Keyset pagination on (user_id, created_at, id): ASC composite serves
        # the DESC scan via PostgreSQL backward index scans, and keeps
        # autogenerate stable (no expression indexes).
        Index("ix_files_user_created_id", "user_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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
    # canonical value also lives in labels/file_labels.
    doc_topic: Mapped[str | None] = mapped_column(String(50), nullable=True)
    extracted_text_preview: Mapped[str | None] = mapped_column(Text, nullable=True)

    labels: Mapped[list[FileLabel]] = relationship(back_populates="file")
