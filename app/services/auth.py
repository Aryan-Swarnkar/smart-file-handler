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
from app.db.models.folder import ROOT_FOLDER_NAME
from app.repositories.folders import FolderRepository
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
INVALID_REFRESH_MESSAGE = "Invalid or expired refresh token. Please log in again."


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
        self._folders = FolderRepository(session)

    async def register(self, email_raw: str, password: str) -> User:
        email = _normalize_email(email_raw)
        existing = await self._users.get_by_email(email)
        if existing is not None:
            raise ConflictError("This email is already registered.", code="email_taken")
        try:
            # ONE transaction: account + its root folder. Spec PHASE 3 — a
            # registered user never browses a rootless drive; if the folder
            # insert fails the user insert rolls back with it.
            user = await self._users.create(
                email,
                hash_password(password),
                storage_quota_bytes=get_settings().default_storage_quota_bytes,
            )
            await self._folders.create(user.id, ROOT_FOLDER_NAME, parent_id=None)
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
        # A login mints the FIRST token of a NEW family: this session's future
        # rotations reuse this uuid, and a reuse attack revokes only it.
        await self._tokens.create(
            user.id, refresh_hash, expires_at, family_id=uuid.uuid4()
        )
        access_token, expires_in = create_access_token(user.id)
        await self._session.commit()
        logger.info("user_login", user_id=str(user.id))
        return LoginResult(user.id, access_token, raw_refresh, expires_in)

    async def resolve_refresh_email(self, raw_refresh: str) -> str | None:
        """Owner email of a presented refresh token (None if unknown).

        The /refresh rate-limit bucket keys on IP+email (spec); the body
        carries no email, so the handler resolves it from the token BEFORE
        rotating. Read-only — no state, no commit.
        """
        return await self._tokens.get_email_by_hash(
            hash_refresh_token(raw_refresh.strip())
        )

    async def refresh(self, raw_refresh: str) -> RefreshResult:
        # strip(): tokens arrive copy-pasted; a trailing newline/space would
        # silently change the SHA-256 and fail a "correct" token.
        row = await self._tokens.get_by_hash(hash_refresh_token(raw_refresh.strip()))
        if row is None:
            raise UnauthorizedError(INVALID_REFRESH_MESSAGE, code="invalid_refresh_token")

        # --- reuse detection: a revoked token presented again means the token
        # was stolen or replayed. Standard practice (OAuth rotation): revoke
        # the whole FAMILY — every live token of THIS login session — forcing
        # re-login on that device. The user's other sessions (other devices)
        # survive: one stolen token must not log everyone out.
        if row.revoked_at is not None:
            nuked = await self._tokens.revoke_family(row.user_id, row.family_id)
            await self._session.commit()
            logger.warning(
                "refresh_token_reuse_detected",
                user_id=str(row.user_id),
                family_id=str(row.family_id),
                revoked_tokens=nuked,
            )
            raise UnauthorizedError(INVALID_REFRESH_MESSAGE, code="invalid_refresh_token")

        now = datetime.now(UTC)
        if row.expires_at <= now:
            await self._tokens.revoke(row)
            await self._session.commit()
            raise UnauthorizedError(INVALID_REFRESH_MESSAGE, code="invalid_refresh_token")

        # --- rotation: old token dies, new one takes its place IN THE SAME
        # family; the chain is auditable via replaced_by_id.
        raw_new, hash_new = new_refresh_token()
        new_row = await self._tokens.create(
            row.user_id,
            hash_new,
            now + timedelta(days=get_settings().REFRESH_TOKEN_EXPIRE_DAYS),
            family_id=row.family_id,
        )
        await self._tokens.revoke(row, replaced_by_id=new_row.id)
        access_token, expires_in = create_access_token(row.user_id)
        await self._session.commit()
        logger.info("refresh_token_rotated", user_id=str(row.user_id))
        return RefreshResult(row.user_id, access_token, raw_new, expires_in)

    async def logout(self, raw_refresh: str) -> None:
        """Idempotent logout (RFC 7009 revocation semantics): ALWAYS succeeds.

        Live token → revoked. Unknown, already-revoked, or garbage → silent
        no-op, same 204. A uniform response (a) can never break the client's
        logout flow — e.g. after refresh rotation or a reuse-detection family
        nuke made the held token stale — and (b) removes the "does this token
        hash exist?" probing oracle a 401 would create.
        """
        token_hash = hash_refresh_token(raw_refresh.strip())
        row = await self._tokens.get_by_hash(token_hash)
        if row is not None and row.revoked_at is None:
            await self._tokens.revoke(row)
            await self._session.commit()
            logger.info("refresh_token_revoked", user_id=str(row.user_id))
        else:
            # Nothing to change; no commit needed. Logged for supportability.
            logger.info("logout_noop", token_known=row is not None)
