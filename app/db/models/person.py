"""persons — one face cluster per person ("Person 1", renamable/mergeable)."""

from __future__ import annotations

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin
from app.db.models.face import FaceInstance


class Person(Base, CreatedAtMixin):
    __tablename__ = "persons"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Incremental cluster centroid (running mean, cosine-normalized); never
    # null in practice: persons are only created when the first face arrives,
    # and empty persons are dropped by curation flows.
    centroid: Mapped[list[float]] = mapped_column(Vector(512), nullable=False)
    face_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Pointer to the highest-det-score face for thumbnails. Deliberately NOT a
    # FK: face_instances already references persons (person_id) — a second FK
    # back would create a cycle that complicates cascade ordering for zero
    # integrity gain. Deletion flows recompute this pointer.
    cover_face_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    faces: Mapped[list[FaceInstance]] = relationship(
        back_populates="person",
        foreign_keys="FaceInstance.person_id",
    )
