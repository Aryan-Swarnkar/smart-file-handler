"""Request/response models for the auth API.

SecretStr keeps passwords out of repr/logs and OpenAPI examples; response
models never carry password material at all.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr

PASSWORD_MIN = 8
PASSWORD_MAX = 128


class RegisterIn(BaseModel):
    email: EmailStr
    # 8+ chars; argon2 handles strength/cost on our side. Clients should send
    # 12+ anyway — the floor is a sanity check, not a security boundary.
    password: SecretStr = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)


class LoginIn(BaseModel):
    email: EmailStr
    password: SecretStr = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    created_at: datetime


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    # Seconds until the ACCESS token expires; clients refresh just before.
    expires_in: int


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=1)


class LogoutIn(BaseModel):
    refresh_token: str = Field(min_length=1)
