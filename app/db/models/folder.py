"""folders — adjacency-list directory tree.

Shape decision (docs/adr/0001): parent_id self-FK + recursive CTE (depth cap
20 enforced in the service layer, not the DB — a DB CHECK cannot see cycles
longer than 1). deleted_at = trash (Phase 10); NULL = live row.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin


class Folder(Base, CreatedAtMixin):
    __tablename__ = "folders"
    __table_args__ = (
        # Drive-style browse: children of one owner's folder, live rows only.
        # Partial (deleted_at IS NULL): the hot path never touches trashed rows.
        Index(
            "ix_folders_owner_parent",
            "owner_id",
            "parent_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Self-referential FK; NULL = the user's root. CASCADE: deleting a folder
    # deletes its whole subtree (Phase 10 purge batches it, DB enforces it).
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"),
        nullable=True,
    )
    # No unique sibling-name constraint: Drive allows two "Notes" folders.
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
