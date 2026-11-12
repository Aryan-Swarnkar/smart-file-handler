"""face_instances — one detected face with its 512-d embedding."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import Float, ForeignKey, Index
from sqlalchemy.dialects.postgresql import ARRAY, DOUBLE_PRECISION
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin

if TYPE_CHECKING:
    from app.db.models.file import File
    from app.db.models.person import Person

# Cosine-distance HNSW ANN index for per-user nearest-neighbor face search.
# - using=hnsw: approximate NN index (exact scan does not exist at 512-d scale)
# - vector_cosine_ops: matches our 1 - (e . c) similarity definition
EMBEDDING_DIM = 512


class FaceInstance(Base, CreatedAtMixin):
    __tablename__ = "face_instances"
    __table_args__ = (
        # ANN index for face_clustering: nearest person centroid per embedding.
        Index(
            "ix_face_instances_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # Ownership-scoped assignment lookups: WHERE user_id = ? AND person_id ...
        Index("ix_face_instances_user_person", "user_id", "person_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # SET NULL: deleting a person (merge/drop) unassigns faces, never deletes them.
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL"),
        nullable=True,
    )
    embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIM), nullable=False
    )
    # (x1, y1, x2, y2) in original pixel coordinates, for crop/thumbnails.
    bbox: Mapped[list[float]] = mapped_column(
        ARRAY(DOUBLE_PRECISION()), nullable=False
    )
    det_score: Mapped[float] = mapped_column(Float, nullable=False)

    file: Mapped[File] = relationship("File", back_populates="faces")
    person: Mapped[Person | None] = relationship(
        "Person",
        back_populates="faces",
        foreign_keys=[person_id],
    )
