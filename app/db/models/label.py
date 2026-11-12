"""labels + file_labels — many-to-many smart categories (things, doc topics)."""

from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy import Float, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin

if TYPE_CHECKING:
    from app.db.models.file import File


class LabelKind(enum.StrEnum):
    THING = "thing"  # CLIP zero-shot: dog, car, food, screenshot...
    DOC_TOPIC = "doc_topic"  # work, study, finance, id, receipt, other


label_kind_enum = SQLAlchemyEnum(
    LabelKind,
    name="label_kind",
    native_enum=True,
    values_callable=lambda e: [m.value for m in e],
)


class Label(Base, CreatedAtMixin):
    __tablename__ = "labels"
    __table_args__ = (
        # Same name can exist per user AND per kind ("work" as thing vs topic).
        # Explicit name: the generic convention would call it uq_labels_user_id.
        UniqueConstraint("user_id", "name", "kind", name="uq_labels_user_id_name_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[LabelKind] = mapped_column(label_kind_enum, nullable=False)


class FileLabel(Base):
    """Association table: a file can carry many labels; labels many files."""

    __tablename__ = "file_labels"
    __table_args__ = (
        # Reverse lookup for "files by label" browse (PK covers file->labels).
        Index("ix_file_labels_label_id", "label_id"),
    )

    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"),
        primary_key=True,
    )
    label_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("labels.id", ondelete="CASCADE"),
        primary_key=True,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # String target: FileLabel imports File would cycle file.py (which imports
    # this module); SQLAlchemy resolves "File" from the registry at configure.
    file: Mapped[File] = relationship("File", back_populates="labels")
