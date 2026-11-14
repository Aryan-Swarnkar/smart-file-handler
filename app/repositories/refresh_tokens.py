"""Refresh-token queries: lookup by hash, family-scoped rotation/revocation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RefreshToken, User


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        result = await self._session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def get_email_by_hash(self, token_hash: str) -> str | None:
        """Email of the token's owner, or None for unknown tokens.

        Rate limiting /refresh keys on IP+email (spec): the presented token
        maps to its owner's email; garbage tokens have no email and share the
        ip-only sentinel bucket. One indexed lookup — the /refresh handler
        resolves BEFORE the service rotates (cheap SELECT join, no state).
        """
        result = await self._session.execute(
            select(User.email)
            .join(RefreshToken, RefreshToken.user_id == User.id)
            .where(RefreshToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        user_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
        *,
        family_id: uuid.UUID,
    ) -> RefreshToken:
        token = RefreshToken(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            family_id=family_id,
        )
        self._session.add(token)
        await self._session.flush()  # assigns id for replaced_by_id links
        return token

    async def revoke(
        self, token: RefreshToken, replaced_by_id: uuid.UUID | None = None
    ) -> None:
        token.revoked_at = datetime.now(UTC)
        token.replaced_by_id = replaced_by_id

    async def revoke_family(self, user_id: uuid.UUID, family_id: uuid.UUID) -> int:
        """Kill every LIVE token of one login session (reuse detection).

        Conditional SET-based UPDATE (idempotent state-machine rule): replayed
        deliveries — a second reused token, a concurrent refresh racing the
        revocation — re-run it as a no-op. Only THIS family dies; the user's
        other devices' sessions survive. Returns rows affected for the log.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(RefreshToken)
                .where(
                    RefreshToken.user_id == user_id,
                    RefreshToken.family_id == family_id,
                    RefreshToken.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(UTC))
            ),
        )
        return result.rowcount or 0
