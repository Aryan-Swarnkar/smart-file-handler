"""Shared FastAPI dependencies: DI wiring + security guards."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import RateLimitedError, UnauthorizedError
from app.core.ratelimit import TokenBucketLimiter, format_retry_after
from app.db.models import User
from app.db.session import get_session
from app.services.auth import AuthService
from app.services.file_service import FileService
from app.services.folder_service import FolderService
from app.services.security import decode_access_token
from app.services.storage_service import StorageService

# auto_error=False: a missing Authorization header should raise OUR envelope
# (401 unauthorized), not FastAPI's default 403 "Not authenticated".
_bearer = HTTPBearer(auto_error=False, bearerFormat="JWT")


def get_redis(request: Request) -> Redis:
    redis: Redis | None = getattr(request.app.state, "redis", None)
    if redis is None:  # lifespan never ran (test misuse) — fail loudly
        raise RuntimeError("Redis pool not initialized; lifespan did not run.")
    return redis


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """Bearer JWT -> User row. The single guard every protected route uses."""
    if credentials is None:
        raise UnauthorizedError("Missing bearer token.")
    user_id = decode_access_token(credentials.credentials)
    user: User | None = await session.get(User, user_id)
    if user is None:
        # Token is valid but the account is gone (deleted): never pretend it
        # belongs to someone else — 401, not 404, to avoid account probing.
        raise UnauthorizedError("Unknown user.")
    return user


async def enforce_auth_rate_limit(
    request: Request,
    redis: Redis,
    bucket: str,
    email: str,
    *,
    action: str,
) -> None:
    """Token-bucket gate for one auth request (spec PHASE 3).

    Called INSIDE the register/login/refresh handlers (P3-5): the bucket key
    includes the body's email, which a parameter-less dependency factory
    cannot see. /refresh passes the token-owner's email resolved from the
    presented token (empty string -> shared ip-only sentinel bucket for
    garbage tokens).

    The per-minute knob is read at CALL time (get_settings(), cache cleared
    per test by conftest) so tests can raise a bucket's capacity via env;
    production reads it once per request — same value every time.

    Behind a proxy `ip` is the proxy's IP unless X-Forwarded-For is trusted;
    that switch lands with the deployment phase.
    """
    settings = get_settings()
    if bucket == "register":
        per_minute = settings.REGISTER_RATELIMIT_PER_MINUTE
    elif bucket == "login":
        per_minute = settings.LOGIN_RATELIMIT_PER_MINUTE
    elif bucket == "refresh":
        per_minute = settings.REFRESH_RATELIMIT_PER_MINUTE
    else:  # pragma: no cover — call sites are typed to the three above
        raise ValueError(f"Unknown rate-limit bucket: {bucket!r}")
    limiter = TokenBucketLimiter(redis, settings.RATELIMIT_NAMESPACE)
    ip = request.client.host if request.client else "unknown"
    result = await limiter.hit(bucket, email, ip, per_minute)
    if not result.allowed:
        # Machines read the Retry-After header; humans read the body.
        raise RateLimitedError(
            message=(
                f"Too many {action} attempts. "
                f"You can try again in {format_retry_after(result.retry_after_seconds)}."
            ),
            details={"retry_after_seconds": result.retry_after_seconds},
            headers={"Retry-After": str(result.retry_after_seconds)},
        )


def auth_service(session: Annotated[AsyncSession, Depends(get_session)]) -> AuthService:
    return AuthService(session)


def get_storage(request: Request) -> StorageService:
    storage: StorageService | None = getattr(request.app.state, "storage", None)
    if storage is None:  # lifespan never ran — fail loudly, not mysteriously
        raise RuntimeError("Storage not initialized; lifespan did not run.")
    return storage


def files_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: Annotated[StorageService, Depends(get_storage)],
) -> FileService:
    # Stateless API: no queue connection — the outbox + relay is the only
    # bridge to Celery (Phase 6); the API enqueues nothing.
    return FileService(session, storage)


def folders_service(session: Annotated[AsyncSession, Depends(get_session)]) -> FolderService:
    return FolderService(session)


CurrentUser = Annotated[User, Depends(get_current_user)]
AuthedService = Annotated[AuthService, Depends(auth_service)]
FilesService = Annotated[FileService, Depends(files_service)]
FoldersService = Annotated[FolderService, Depends(folders_service)]
