"""Authentication and token utilities for Smart File Handler.

Expected settings import: app.core.config.get_settings
If your settings module has a different filename, update that import.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from jwt.exceptions import InvalidTokenError
from pwdlib import PasswordHash

from app.core.config import get_settings

settings = get_settings()

# pwdlib's recommended hasher currently uses Argon2id.
password_hasher = PasswordHash.recommended()

# Keep this allow-list fixed. Do not derive it from the token's untrusted header.
JWT_ALGORITHMS = ["HS256"]


def hash_password(password: str) -> str:
    """Return an Argon2id password hash suitable for database storage."""
    return password_hasher.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Return True when the password matches the stored Argon2id hash."""
    return password_hasher.verify(plain_password, hashed_password)


def create_access_token(user_id: str) -> str:
    """Create a short-lived HS256 access JWT for one user."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )

    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": expires_at,
        "type": "access",
    }

    return jwt.encode(
        payload,
        settings.SECRET_KEY,
        algorithm="HS256",
    )


def decode_access_token(token: str) -> dict:
    """Verify an access JWT and return its claims.

    PyJWT raises InvalidTokenError (or a subclass) for malformed, expired,
    incorrectly signed, or otherwise invalid tokens. Callers should translate
    that into an HTTP 401 response.
    """
    payload = jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=JWT_ALGORITHMS,
        options={"require": ["exp", "sub"]},
    )

    if payload.get("type") != "access":
        raise InvalidTokenError("Token is not an access token")

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise InvalidTokenError("Token subject is missing or invalid")

    return payload


def create_refresh_token() -> str:
    """Generate a cryptographically secure, URL-safe opaque refresh token."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(refresh_token: str) -> str:
    """Return the SHA-256 hex digest to store instead of the raw token."""
    return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()