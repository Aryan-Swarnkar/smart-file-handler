"""refresh_tokens — rotating refresh tokens with reuse detection.

A token belongs to a login SESSION via family_id: one login mints the first
token of a family; every rotation within that session reuses the family_id.
Reuse detection revokes the whole FAMILY (the session's other live tokens),
not all tokens of the user — logging in on phone+desktop kills only the
compromised device's session (P3 decision).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin


class RefreshToken(Base, CreatedAtMixin):
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        # Revoke-the-family UPDATE filters (user_id, family_id, revoked_at IS
        # NULL); its leftmost prefix also serves the per-user token listing,
        # which is why the old single-column user_id index is dropped.
        Index("ix_refresh_tokens_user_family", "user_id", "family_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The login session this token belongs to. A PLAIN UUID, deliberately no
    # FK to refresh_tokens.id: the family root is just the first token's
    # peer — deleting that row (a future expired-token sweeper) must never
    # cascade away the still-live descendants, and referential integrity is
    # already guaranteed by user_id. NOT NULL with no default: a creator that
    # forgets it fails loudly instead of minting an un-groupable token.
    family_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    # Only the SHA-256 of the raw token is stored; leaks are useless to replay.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Rotation chain: which token replaced this one. Self-referential FK.
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
