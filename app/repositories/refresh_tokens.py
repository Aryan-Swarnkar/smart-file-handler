"""Refresh-token queries: lookup by hash, rotation, family revocation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RefreshToken


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        result = await self._session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def create(
        self, user_id: uuid.UUID, token_hash: str, expires_at: datetime
    ) -> RefreshToken:
        token = RefreshToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
        self._session.add(token)
        await self._session.flush()  # assigns id for replaced_by_id links
        return token

    async def revoke(
        self, token: RefreshToken, replaced_by_id: uuid.UUID | None = None
    ) -> None:
        token.revoked_at = datetime.now(UTC)
        token.replaced_by_id = replaced_by_id

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        """Nuke every live token of a user (refresh-token reuse detection).

        A SET-based UPDATE: one round-trip even if the user has hundreds of
        sessions. Returns rows affected for the audit log.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(RefreshToken)
                .where(
                    RefreshToken.user_id == user_id,
                    RefreshToken.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(UTC))
            ),
        )
        return result.rowcount or 0
