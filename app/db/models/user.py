"""users — one row per registered account. No admin role exists."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, String
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin

# 5 GiB as a byte literal: a DB-side floor so any INSERT (even tests) works
# without knowing settings. Registration overrides it from env (D2) — the
# server default exists only so the column never blocks an insert.
DEFAULT_QUOTA_SERVER_DEFAULT = "5368709120"


class User(Base, CreatedAtMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # citext: case-insensitive uniqueness enforced by the DB (two registrations
    # differing only in case collide), not by service-layer discipline.
    email: Mapped[str] = mapped_column(CITEXT(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # Quota accounting (Phase 5 enforces it atomically):
    # storage_used_bytes is a running sum of live files' sizes; reservation is
    # a conditional UPDATE, never a read-modify-write.
    storage_quota_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=DEFAULT_QUOTA_SERVER_DEFAULT,
    )
    storage_used_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="0",
    )
