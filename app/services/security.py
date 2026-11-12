"""Password hashing, JWT access tokens, refresh-token material.

Pure functions only — no DB, no HTTP. Everything here is CPU-bound but short
(argon2 ~0.1s, HMAC signing ~µs), so it is safe to call directly from async
handlers; the heavyweight candidate (async worker offload) is noted in docs.
"""

from __future__ import annotations

import contextlib
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, VerifyMismatchError

from app.core.config import get_settings
from app.core.errors import UnauthorizedError

# argon2id defaults: 64 MiB memory, 3 iterations, 4 lanes — OWASP-aligned for
# interactive logins. Same hasher instance reuses the precomputed params.
_password_hasher = PasswordHasher()

# Constant-cost decoy for the "unknown email" path: verify against a throwaway
# hash so real-unknown and wrong-password logins take the same wall time and
# response timing cannot reveal whether an email exists (user enumeration).
_DUMMY_HASH = PasswordHasher().hash("timing-equalizer-not-a-real-password")


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Argon2Error:
        # Malformed/corrupted stored hash: treat as failed login, never 500.
        return False


def dummy_verify() -> None:
    with contextlib.suppress(VerifyMismatchError):
        _password_hasher.verify(_DUMMY_HASH, "wrong-on-purpose")


def create_access_token(user_id: uuid.UUID) -> tuple[str, int]:
    """HS256 JWT. Returns (token, expires_in_seconds)."""
    settings = get_settings()
    now = datetime.now(UTC)
    expires_in = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
        "typ": "access",  # never accept a token of another type
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, expires_in


def decode_access_token(token: str) -> uuid.UUID:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("Token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("Invalid token.") from exc
    if payload.get("typ") != "access":
        raise UnauthorizedError("Invalid token.")
    try:
        return uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise UnauthorizedError("Invalid token subject.") from exc


def new_refresh_token() -> tuple[str, str]:
    """256-bit opaque token; only its SHA-256 ever touches the database."""
    raw = secrets.token_urlsafe(32)  # ~256 bits of entropy
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
