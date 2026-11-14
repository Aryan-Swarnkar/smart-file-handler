"""Auth API: register, login, refresh, logout, me.

Rate limiting (Phase 3): register/login/refresh pass through an atomic Redis
Lua token bucket keyed by IP+email (deps.enforce_auth_rate_limit). The call
sits in the handlers because the bucket key needs the body's email. /refresh
resolves the presented token's owner first (unknown tokens share the
ip-only sentinel bucket). Password strength beyond min length is argon2's
job; enumeration is defeated by identical 401 messages + timing-equalized
unknown-email handling.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from redis.asyncio import Redis

from app.api.deps import (
    AuthedService,
    CurrentUser,
    enforce_auth_rate_limit,
    get_redis,
)
from app.schemas.auth import (
    LoginIn,
    LogoutIn,
    RefreshIn,
    RegisterIn,
    TokenPair,
    UserOut,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
)
async def register(
    payload: RegisterIn,
    request: Request,
    redis: Annotated[Redis, Depends(get_redis)],
    service: AuthedService,
) -> UserOut:
    await enforce_auth_rate_limit(
        request,
        redis,
        "register",
        payload.email,
        action="registration",
    )
    user = await service.register(payload.email, payload.password.get_secret_value())
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenPair, summary="Exchange credentials for tokens")
async def login(
    payload: LoginIn,
    request: Request,
    redis: Annotated[Redis, Depends(get_redis)],
    service: AuthedService,
) -> TokenPair:
    await enforce_auth_rate_limit(
        request,
        redis,
        "login",
        payload.email,
        action="login",
    )
    result = await service.login(payload.email, payload.password.get_secret_value())
    return TokenPair(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
    )


@router.post("/refresh", response_model=TokenPair, summary="Rotate the refresh token")
async def refresh(
    payload: RefreshIn,
    request: Request,
    redis: Annotated[Redis, Depends(get_redis)],
    service: AuthedService,
) -> TokenPair:
    # The body carries no email: resolve the token's owner BEFORE rotating so
    # the bucket keys on IP+email per spec. None -> "" = shared ip-only
    # sentinel bucket (hash("") is a stable constant).
    email = await service.resolve_refresh_email(payload.refresh_token) or ""
    await enforce_auth_rate_limit(
        request,
        redis,
        "refresh",
        email,
        action="refresh",
    )
    result = await service.refresh(payload.refresh_token)
    return TokenPair(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Revoke one session")
async def logout(
    payload: LogoutIn,
    service: AuthedService,
) -> None:
    await service.logout(payload.refresh_token)


@router.get("/me", response_model=UserOut, summary="Who am I (bearer token)")
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
