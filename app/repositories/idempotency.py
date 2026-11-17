"""Idempotency-Key replay for POST /files/init.

Same key + same body → the stored 201 response is replayed VERBATIM (the
presigned url inside may have aged past its own expiry — the correct client
recovery is to complete, or init again with a fresh key; re-signing on
replay would silently break the "stored response" contract). Same key +
different body → 422: the request_hash catches body drift.

Concurrency: a per-(user, key) transaction-scoped advisory lock serializes
parallel inits — the loser of the race blocks until the winner commits, then
reads the stored row and replays it. No polling, no unique-violation retries.

Failures are NEVER recorded: the idempotency row commits in the same
transaction as the upload, so a rolled-back init leaves no row and a retry
re-executes honestly.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IdempotencyKey


def request_hash(filename: str, content_type: str, size: int, folder_id: str) -> str:
    """sha256 over a canonical (key-sorted) join of the init body. folder_id
    participates: the same bytes into a different folder is a DIFFERENT
    request and must not replay the first one's response."""
    parts = [
        f"filename={filename}",
        f"content_type={content_type}",
        f"size={size}",
        f"folder_id={folder_id}",
    ]
    canonical = "|".join(sorted(parts))
    return hashlib.sha256(canonical.encode()).hexdigest()


def lock_key(user_id: uuid.UUID, key: str) -> int:
    """Stable 64-bit advisory-lock key for (user, key) — first 8 digest bytes,
    big-endian, computed in Python (no SQL casting games)."""
    digest = hashlib.sha256(f"{user_id}:{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class IdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock(self, user_id: uuid.UUID, key: str) -> None:
        """Serialize same-(user,key) inits until this transaction ends."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key(user_id, key)}
        )

    async def get_for_replay(self, user_id: uuid.UUID, key: str) -> IdempotencyKey | None:
        stmt = select(IdempotencyKey).where(
            IdempotencyKey.user_id == user_id, IdempotencyKey.key == key
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def store(
        self,
        *,
        user_id: uuid.UUID,
        key: str,
        request_hash: str,
        response_status: int,
        response_body: dict[str, Any],
        expires_at: datetime,
    ) -> IdempotencyKey:
        row = IdempotencyKey(
            user_id=user_id,
            key=key,
            request_hash=request_hash,
            response_status=response_status,
            response_body=response_body,
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row
