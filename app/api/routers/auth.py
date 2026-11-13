"""Auth API: register, login, refresh, logout, me.

Rate limiting: login and register are throttled per client IP (env-tunable).
Password strength beyond min length is argon2's job; enumeration is defeated
by identical 401 messages + timing-equalized unknown-email handling.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import AuthedService, CurrentUser, rate_limit
from app.core.config import get_settings
from app.schemas.auth import (
    LoginIn,
    LogoutIn,
    RefreshIn,
    RegisterIn,
    TokenPair,
    UserOut,
)

_settings = get_settings()

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
)
async def register(
    payload: RegisterIn,
    service: AuthedService,
    _rl: Annotated[
        None,
        Depends(
            rate_limit(
                "auth:register", _settings.REGISTER_RATELIMIT_PER_MINUTE, action="registration"
            )
        ),
    ],
) -> UserOut:
    user = await service.register(payload.email, payload.password.get_secret_value())
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenPair, summary="Exchange credentials for tokens")
async def login(
    payload: LoginIn,
    service: AuthedService,
    _rl: Annotated[
        None,
        Depends(
            rate_limit("auth:login", _settings.LOGIN_RATELIMIT_PER_MINUTE, action="login")
        ),
    ],
) -> TokenPair:
    result = await service.login(payload.email, payload.password.get_secret_value())
    return TokenPair(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
    )


@router.post("/refresh", response_model=TokenPair, summary="Rotate the refresh token")
async def refresh(
    payload: RefreshIn,
    service: AuthedService,
) -> TokenPair:
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
