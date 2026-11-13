"""P2 auth integration tests.

Runs against the real compose stack (postgres :5433 + redis :6379). Auth
operations COMMIT (real users accumulate in the DB across runs) — so every
test uses a unique email to stay hermetic. Rate-limit counters are flushed
per test via the per-run namespace.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from argon2 import PasswordHasher
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db.models import RefreshToken, User
from app.services.security import decode_access_token, hash_refresh_token

_ph = PasswordHasher()


def _email() -> str:
    return f"user-{uuid.uuid4().hex[:10]}@example.com"


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(client: AsyncClient, email: str) -> Response:
    return await client.post(
        "/auth/register", json={"email": email, "password": "correct-horse-1"}
    )


async def _login(client: AsyncClient, email: str) -> Response:
    return await client.post(
        "/auth/login", json={"email": email, "password": "correct-horse-1"}
    )


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """NullPool per-test engine (same pattern as test_db_integration)."""
    engine = create_async_engine(get_settings().DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


# --- register ---------------------------------------------------------------


async def test_register_creates_user(client: AsyncClient, db: AsyncSession) -> None:
    email = _email()
    resp = await _register(client, email)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == email  # exact case round-trip
    assert uuid.UUID(body["id"])  # valid UUID
    assert body["created_at"]

    # DB truth: argon2 hash, not the raw password.
    user = (await db.execute(select(User).where(User.email == email))).scalar_one()
    assert user.password_hash.startswith("$argon2")
    assert _ph.verify(user.password_hash, "correct-horse-1")


async def test_register_duplicate_email_case_insensitive(client: AsyncClient) -> None:
    email = _email()
    first = await _register(client, email)
    assert first.status_code == 201
    dup = await _register(client, email.upper())
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "email_taken"


async def test_register_invalid_email_422(client: AsyncClient) -> None:
    resp = await client.post(
        "/auth/register", json={"email": "not-an-email", "password": "correct-horse-1"}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_register_short_password_422(client: AsyncClient) -> None:
    resp = await client.post(
        "/auth/register", json={"email": _email(), "password": "short"}
    )
    assert resp.status_code == 422


# --- login --------------------------------------------------------------------


async def test_login_success_returns_pair(
    client: AsyncClient, db: AsyncSession
) -> None:
    email = _email()
    await _register(client, email)
    resp = await _login(client, email)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 15 * 60

    # Access token payload names the user; refresh row exists and is live.
    user_id = decode_access_token(body["access_token"])
    user = await db.get(User, user_id)
    assert user is not None and user.email == email
    rows = (
        await db.execute(
            select(RefreshToken).where(RefreshToken.user_id == user_id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].revoked_at is None
    assert rows[0].token_hash == hash_refresh_token(body["refresh_token"])


async def test_login_wrong_password_401_generic(client: AsyncClient) -> None:
    email = _email()
    await _register(client, email)
    resp = await client.post(
        "/auth/login", json={"email": email, "password": "wrong-password-1"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_credentials"


async def test_login_unknown_email_same_response_as_wrong_password(
    client: AsyncClient,
) -> None:
    """Enumeration defense: unknown email and bad password are indistinguishable."""
    await _register(client, _email())
    unknown = await client.post(
        "/auth/login",
        json={"email": _email(), "password": "wrong-password-1"},
    )
    wrong_pw = await client.post(
        "/auth/login",
        json={"email": _email(), "password": "wrong-password-1"},
    )
    assert unknown.status_code == wrong_pw.status_code == 401
    assert unknown.json()["error"] == wrong_pw.json()["error"]


# --- me -----------------------------------------------------------------------


async def test_me_requires_token(client: AsyncClient) -> None:
    resp = await client.get("/auth/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_me_garbage_token_401(client: AsyncClient) -> None:
    resp = await client.get("/auth/me", headers=_auth_header("garbage.token.here"))
    assert resp.status_code == 401


async def test_me_returns_caller(client: AsyncClient) -> None:
    email = _email()
    await _register(client, email)
    login = await _login(client, email)
    token = login.json()["access_token"]
    resp = await client.get("/auth/me", headers=_auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["email"] == email


# --- refresh: rotation + reuse detection ----------------------------------------


async def test_refresh_rotates_and_revokes_old(
    client: AsyncClient, db: AsyncSession
) -> None:
    email = _email()
    await _register(client, email)
    login = (await _login(client, email)).json()
    old_refresh = login["refresh_token"]

    resp = await client.post("/auth/refresh", json={"refresh_token": old_refresh})
    assert resp.status_code == 200, resp.text
    new_pair = resp.json()
    assert new_pair["refresh_token"] != old_refresh

    old_row = (
        await db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == hash_refresh_token(old_refresh)
            )
        )
    ).scalar_one()
    new_row = (
        await db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == hash_refresh_token(new_pair["refresh_token"])
            )
        )
    ).scalar_one()
    assert old_row.revoked_at is not None
    assert old_row.replaced_by_id == new_row.id  # audit chain intact
    assert new_row.revoked_at is None

    # Old token no longer refreshes -> 401.
    replay = await client.post("/auth/refresh", json={"refresh_token": old_refresh})
    assert replay.status_code == 401


async def test_refresh_reuse_revokes_entire_family(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Presenting a rotated-away token must kill every live session."""
    email = _email()
    await _register(client, email)
    first = (await _login(client, email)).json()  # token A
    second = (
        await client.post(
            "/auth/refresh", json={"refresh_token": first["refresh_token"]}
        )
    ).json()  # token B (A is now revoked)

    # Reuse A:
    reuse = await client.post(
        "/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert reuse.status_code == 401

    # Family nuked: B (still fresh) must ALSO be dead now.
    dead = await client.post(
        "/auth/refresh", json={"refresh_token": second["refresh_token"]}
    )
    assert dead.status_code == 401

    user = (await db.execute(select(User).where(User.email == email))).scalar_one()
    rows = (
        await db.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))
    ).scalars().all()
    assert len(rows) >= 2
    assert all(r.revoked_at is not None for r in rows)


async def test_refresh_unknown_token_401(client: AsyncClient) -> None:
    resp = await client.post("/auth/refresh", json={"refresh_token": "no-such-token"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_refresh_token"


# --- logout ---------------------------------------------------------------------


async def test_logout_revokes_session(client: AsyncClient) -> None:
    email = _email()
    await _register(client, email)
    login = (await _login(client, email)).json()

    resp = await client.post("/auth/logout", json={"refresh_token": login["refresh_token"]})
    assert resp.status_code == 204

    # Refresh after logout fails; access token still works until it expires
    # (stateless JWT — documented trade-off, short expiry is the bound).
    dead = await client.post(
        "/auth/refresh", json={"refresh_token": login["refresh_token"]}
    )
    assert dead.status_code == 401
    still_me = await client.get("/auth/me", headers=_auth_header(login["access_token"]))
    assert still_me.status_code == 200


async def test_logout_is_idempotent_rfc7009(client: AsyncClient, db: AsyncSession) -> None:
    """Logout NEVER fails: live, rotated-away, or unknown token -> 204."""
    email = _email()
    await _register(client, email)
    login = (await _login(client, email)).json()
    live_token = login["refresh_token"]

    # Rotate it away: old token is now revoked, new one is live.
    rotated = (
        await client.post("/auth/refresh", json={"refresh_token": live_token})
    ).json()

    # 1) Stale (revoked) token -> 204, not 401. THE bug users hit before.
    stale = await client.post("/auth/logout", json={"refresh_token": live_token})
    assert stale.status_code == 204
    # 2) Unknown garbage -> 204 (also removes the token-existence oracle).
    unknown = await client.post("/auth/logout", json={"refresh_token": "no-such-token"})
    assert unknown.status_code == 204
    # 3) Whitespace-padded copy-paste -> 204, and the LIVE token gets revoked.
    padded = await client.post(
        "/auth/logout", json={"refresh_token": f"  {rotated['refresh_token']} \n"}
    )
    assert padded.status_code == 204

    # DB truth: every token of this user is revoked; nothing left live.
    user = (await db.execute(select(User).where(User.email == email))).scalar_one()
    rows = (
        await db.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))
    ).scalars().all()
    assert rows, "expected at least two refresh rows"
    assert all(r.revoked_at is not None for r in rows)


async def test_refresh_accepts_whitespace_padded_token(client: AsyncClient) -> None:
    email = _email()
    await _register(client, email)
    login = (await _login(client, email)).json()
    resp = await client.post(
        "/auth/refresh", json={"refresh_token": f" {login['refresh_token']}\n"}
    )
    assert resp.status_code == 200, resp.text


# --- rate limiting ----------------------------------------------------------------


async def test_login_rate_limited_429(client: AsyncClient) -> None:
    limit = get_settings().LOGIN_RATELIMIT_PER_MINUTE
    email = _email()
    await _register(client, email)
    last = None
    for _i in range(limit + 2):  # push past the window
        last = await client.post(
            "/auth/login", json={"email": email, "password": "wrong-password-1"}
        )
    assert last is not None and last.status_code == 429
    assert last.json()["error"]["code"] == "rate_limited"
    retry_after = int(last.headers["retry-after"])
    assert 0 <= retry_after <= 60
    # Human-readable body: exact seconds in the message + machine field.
    body = last.json()["error"]
    assert body["details"]["retry_after_seconds"] == retry_after
    assert body["message"] == (
        f"Too many login attempts. You can try again in {retry_after} seconds."
    )


async def test_register_rate_limited_429(client: AsyncClient) -> None:
    limit = get_settings().REGISTER_RATELIMIT_PER_MINUTE
    last = None
    for _i in range(limit + 2):
        last = await _register(client, _email())
    assert last is not None and last.status_code == 429
    assert "retry-after" in last.headers
    assert "registration attempts" in last.json()["error"]["message"]
