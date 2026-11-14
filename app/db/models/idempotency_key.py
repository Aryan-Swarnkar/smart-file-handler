"""idempotency_keys — Idempotency-Key replay for POST /files/init.

Same key + same body -> the stored response is replayed verbatim; same key +
different body -> 422 (the hash catches body drift). Rows expire (sweeper,
Phase 6): retries past the TTL are simply a new upload.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin


class IdempotencyKey(Base, CreatedAtMixin):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        # A key is scoped to its user: two users may reuse the same header
        # value without colliding.
        UniqueConstraint("user_id", "key", name="uq_idempotency_keys_user_key"),
        # Sweeper (Phase 6) deletes expired rows by this index.
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    # sha256 hex of the canonical request body — detects "same key, new body".
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    # The exact response body replayed on retry (jsonb: schema evolves freely).
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
