"""AuthService: register / login / refresh (rotation + reuse detection) / logout.

Transaction rule: one commit per service operation; repositories only flush.
Every failure path either commits its side-effect (reuse-detection nuke) or
rolls back cleanly — no half-issued token pairs ever leave this class.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError, UnauthorizedError
from app.core.logging import get_logger
from app.db.models import User
from app.repositories.refresh_tokens import RefreshTokenRepository
from app.repositories.users import UserRepository
from app.services.security import (
    create_access_token,
    dummy_verify,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)

logger = get_logger(__name__)

INVALID_LOGIN_MESSAGE = "Invalid email or password."
INVALID_REFRESH_MESSAGE = "Invalid refresh token."


class LoginResult(NamedTuple):
    user_id: uuid.UUID
    access_token: str
    refresh_token: str  # RAW token — returned once, never persisted
    expires_in: int


class RefreshResult(NamedTuple):
    user_id: uuid.UUID
    access_token: str
    refresh_token: str
    expires_in: int


def _normalize_email(email: str) -> str:
    # Emails are case-insensitive in practice; we lowercase everything and the
    # unique index makes duplicates impossible (no citext dependency).
    return email.strip().lower()


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._tokens = RefreshTokenRepository(session)

    async def register(self, email_raw: str, password: str) -> User:
        email = _normalize_email(email_raw)
        existing = await self._users.get_by_email(email)
        if existing is not None:
            raise ConflictError("This email is already registered.", code="email_taken")
        try:
            user = await self._users.create(email, hash_password(password))
            await self._session.commit()
        except IntegrityError:
            # Lost a race against a concurrent register (unique index fired):
            # same client-visible outcome as the pre-check above.
            await self._session.rollback()
            raise ConflictError(
                "This email is already registered.", code="email_taken"
            ) from None
        logger.info("user_registered", user_id=str(user.id))
        return user

    async def login(self, email_raw: str, password: str) -> LoginResult:
        email = _normalize_email(email_raw)
        user = await self._users.get_by_email(email)
        if user is None:
            # Burn the same argon2 cost as a real verify BEFORE failing, so
            # timing cannot distinguish "unknown email" from "wrong password".
            dummy_verify()
            raise UnauthorizedError(INVALID_LOGIN_MESSAGE, code="invalid_credentials")
        if not verify_password(user.password_hash, password):
            raise UnauthorizedError(INVALID_LOGIN_MESSAGE, code="invalid_credentials")

        raw_refresh, refresh_hash = new_refresh_token()
        expires_at = datetime.now(UTC) + timedelta(days=get_settings().REFRESH_TOKEN_EXPIRE_DAYS)
        await self._tokens.create(user.id, refresh_hash, expires_at)
        access_token, expires_in = create_access_token(user.id)
        await self._session.commit()
        logger.info("user_login", user_id=str(user.id))
        return LoginResult(user.id, access_token, raw_refresh, expires_in)

    async def refresh(self, raw_refresh: str) -> RefreshResult:
        row = await self._tokens.get_by_hash(hash_refresh_token(raw_refresh))
        if row is None:
            raise UnauthorizedError(INVALID_REFRESH_MESSAGE, code="invalid_refresh_token")

        # --- reuse detection: a revoked token presented again means the token
        # was stolen or replayed. Standard practice (OAuth rotation): revoke
        # the user's ENTIRE live token family, forcing re-login everywhere.
        if row.revoked_at is not None:
            nuked = await self._tokens.revoke_all_for_user(row.user_id)
            await self._session.commit()
            logger.warning(
                "refresh_token_reuse_detected",
                user_id=str(row.user_id),
                revoked_tokens=nuked,
            )
            raise UnauthorizedError(INVALID_REFRESH_MESSAGE, code="invalid_refresh_token")

        now = datetime.now(UTC)
        if row.expires_at <= now:
            await self._tokens.revoke(row)
            await self._session.commit()
            raise UnauthorizedError(INVALID_REFRESH_MESSAGE, code="invalid_refresh_token")

        # --- rotation: old token dies, new one takes its place; the chain is
        # auditable via replaced_by_id.
        raw_new, hash_new = new_refresh_token()
        new_row = await self._tokens.create(
            row.user_id,
            hash_new,
            now + timedelta(days=get_settings().REFRESH_TOKEN_EXPIRE_DAYS),
        )
        await self._tokens.revoke(row, replaced_by_id=new_row.id)
        access_token, expires_in = create_access_token(row.user_id)
        await self._session.commit()
        logger.info("refresh_token_rotated", user_id=str(row.user_id))
        return RefreshResult(row.user_id, access_token, raw_new, expires_in)

    async def logout(self, raw_refresh: str) -> None:
        row = await self._tokens.get_by_hash(hash_refresh_token(raw_refresh))
        # Unknown or already-revoked: reject rather than silently succeed so a
        # client cannot "log out" with garbage and believe it secured anything.
        if row is None or row.revoked_at is not None:
            raise UnauthorizedError(INVALID_REFRESH_MESSAGE, code="invalid_refresh_token")
        await self._tokens.revoke(row)
        await self._session.commit()
        logger.info("refresh_token_revoked", user_id=str(row.user_id))
