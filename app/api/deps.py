"""Shared FastAPI dependencies: DI wiring + security guards."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import RateLimitedError, UnauthorizedError
from app.db.models import User
from app.db.session import get_session
from app.services.auth import AuthService
from app.services.file_service import FileService
from app.services.rate_limit import RateLimiter
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


def format_retry_after(seconds: int) -> str:
    """Human wording for the 429 body: '29 seconds' / '2 minutes'."""
    if seconds < 60:
        unit = "second" if seconds == 1 else "seconds"
        return f"{seconds} {unit}"
    minutes = max(1, seconds // 60)  # partial minutes round up to 1+
    unit = "minute" if minutes == 1 else "minutes"
    return f"{minutes} {unit}"


def rate_limit(
    scope: str, limit: int, window_seconds: int = 60, action: str = "request"
) -> Callable[..., Awaitable[None]]:
    """Dependency factory: fixed-window limiter keyed by client IP + scope.

    `limit` is read once at import time (same import-time settings pattern as
    the DB engine URL); counters live in Redis, shared by all replicas.
    """

    async def _dependency(
        request: Request,
        redis: Annotated[Redis, Depends(get_redis)],
    ) -> None:
        limiter = RateLimiter(redis, get_settings().RATELIMIT_NAMESPACE)
        # Behind a proxy this is the proxy's IP unless X-Forwarded-For is
        # trusted; that switch lands with the deployment phase (P9).
        ip = request.client.host if request.client else "unknown"
        result = await limiter.hit(scope, ip, limit, window_seconds)
        if not result.allowed:
            # Machines read the Retry-After header; humans read the body.
            raise RateLimitedError(
                message=(
                    f"Too many {action} attempts. "
                    f"You can try again in {format_retry_after(result.reset_after)}."
                ),
                details={"retry_after_seconds": result.reset_after},
                headers={"Retry-After": str(result.reset_after)},
            )

    return _dependency


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


CurrentUser = Annotated[User, Depends(get_current_user)]
AuthedService = Annotated[AuthService, Depends(auth_service)]
FilesService = Annotated[FileService, Depends(files_service)]
