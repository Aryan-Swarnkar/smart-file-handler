"""share_links — anonymous public links to one file OR one folder.

Only the sha256 of the token is stored; the plaintext token is shown to the
creator exactly once (Phase 8). Viewer-only: a link never grants editor.
`allow_download` defaults to True (Drive semantics — a shared file can be
downloaded); Phase 8 can flip the default after product review.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin


class ShareLink(Base, CreatedAtMixin):
    __tablename__ = "share_links"
    __table_args__ = (
        CheckConstraint(
            "(file_id IS NULL) <> (folder_id IS NULL)",
            name="ck_share_links_exactly_one_target",
        ),
        # Phase 8 "links on this folder/file" listing (reverse lookup).
        Index("ix_share_links_folder", "folder_id"),
        Index("ix_share_links_file", "file_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        # Links belong to their creator; their targets cascade away with the
        # creator's files/folders anyway, so the whole link dies with them.
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # sha256 hex of secrets.token_urlsafe(32). Unique index = the lookup path
    # for anonymous /s/{token} requests (Phase 8).
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"),
        nullable=True,
    )
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Optional protections: argon2 hash of the link password, absolute expiry,
    # and revocation timestamp. NULL = no password / no expiry / not revoked.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    allow_download: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
