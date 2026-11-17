"""Quota accounting — atomic reservation, never read-modify-write.

One conditional UPDATE decides the race inside PostgreSQL: two concurrent
reservations of the last free bytes cannot both win, no application-level
locking needed. Release is guarded by `storage_used_bytes >= n` so a double
release (delete after an already-released failed complete) can never drive
the counter negative or steal another reservation.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User


class QuotaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def reserve(self, owner_id: uuid.UUID, size_bytes: int) -> bool:
        """Add `size_bytes` to the owner's used counter IFF it stays within
        the quota. True = reserved (row updated); False = would exceed."""
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(User)
                .where(
                    User.id == owner_id,
                    User.storage_used_bytes + size_bytes <= User.storage_quota_bytes,
                )
                .values(storage_used_bytes=User.storage_used_bytes + size_bytes)
            ),
        )
        return bool(result.rowcount == 1)

    async def release(self, owner_id: uuid.UUID, size_bytes: int) -> bool:
        """Subtract `size_bytes` IFF the counter holds at least that much.
        A 0-byte release is a valid no-op (row still updated)."""
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(User)
                .where(
                    User.id == owner_id,
                    User.storage_used_bytes >= size_bytes,
                )
                .values(storage_used_bytes=User.storage_used_bytes - size_bytes)
            ),
        )
        return bool(result.rowcount == 1)

    async def used_bytes(self, owner_id: uuid.UUID) -> int | None:
        return (
            await self._session.execute(
                select(User.storage_used_bytes).where(User.id == owner_id)
            )
        ).scalar_one_or_none()
