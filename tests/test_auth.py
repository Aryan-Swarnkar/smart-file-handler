"""P2 auth integration tests.

Runs against the real compose stack (postgres :5433 + redis :6379). Auth
operations COMMIT (real users accumulate in the DB across runs) — so every
test uses a unique email to stay hermetic. Rate-limit counters are flushed
per test via the per-run namespace.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
from argon2 import PasswordHasher
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.ratelimit import format_retry_after
from app.db.models import Folder, RefreshToken, User
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


async def test_register_creates_root_folder(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Spec PHASE 3: registration creates user + root folder in ONE transaction."""
    email = _email()
    resp = await _register(client, email)
    assert resp.status_code == 201, resp.text
    user = (await db.execute(select(User).where(User.email == email))).scalar_one()
    folders = (
        await db.execute(select(Folder).where(Folder.owner_id == user.id))
    ).scalars().all()
    assert len(folders) == 1
    assert folders[0].name == "My Files"
    assert folders[0].parent_id is None  # parent_id NULL is the root marker


async def test_concurrent_register_same_email_exactly_one_success(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec PHASE 3: N racing registrations -> exactly one 201, the rest 409.

    Every request runs its own transaction; the DB unique index is the
    arbiter. Losers are caught by the service's IntegrityError path and must
    leave NO partial rows (user+folder commit atomically or not at all).
    The register bucket is raised so the RATE LIMITER doesn't 429 the race
    participants before they reach the DB (capacity 5 < 8 racers otherwise).
    monkeypatch restores the env; the explicit cache_clear is required because
    the client fixture's lifespan startup re-populated the cache (with the
    default 5) BEFORE this body ran.
    """
    monkeypatch.setenv("REGISTER_RATELIMIT_PER_MINUTE", "100")
    get_settings.cache_clear()
    email = _email()
    responses = await asyncio.gather(
        *[
            client.post(
                "/auth/register", json={"email": email, "password": "correct-horse-1"}
            )
            for _ in range(8)
        ]
    )
    created = [r for r in responses if r.status_code == 201]
    conflicts = [r for r in responses if r.status_code == 409]
    assert len(created) == 1, [r.status_code for r in responses]
    assert len(conflicts) == 7, [r.status_code for r in responses]
    users = (
        await db.execute(select(User).where(User.email == email))
    ).scalars().all()
    assert len(users) == 1
    # The atomic registration also means exactly ONE root folder, no orphans.
    folders = (
        await db.execute(
            select(Folder).where(Folder.owner_id == users[0].id)
        )
    ).scalars().all()
    assert len(folders) == 1


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


async def test_refresh_reuse_spares_other_families(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Reuse revokes ONE login session (the family), not the user's others."""
    email = _email()
    await _register(client, email)
    family_a = (await _login(client, email)).json()
    family_b = (await _login(client, email)).json()  # second device/session

    # Rotate family A away normally: A1 -> A2 (A1 is now revoked).
    rotated = (
        await client.post(
            "/auth/refresh", json={"refresh_token": family_a["refresh_token"]}
        )
    ).json()

    # Reuse the rotated-away A1 -> family A dies...
    reuse = await client.post(
        "/auth/refresh", json={"refresh_token": family_a["refresh_token"]}
    )
    assert reuse.status_code == 401

    # ...family B (the other device) still refreshes: 200, not 401.
    survivor = await client.post(
        "/auth/refresh", json={"refresh_token": family_b["refresh_token"]}
    )
    assert survivor.status_code == 200, survivor.text
    survivor_token = survivor.json()["refresh_token"]

    user = (await db.execute(select(User).where(User.email == email))).scalar_one()
    rows = (
        await db.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))
    ).scalars().all()
    row_a = next(r for r in rows if r.token_hash == hash_refresh_token(family_a["refresh_token"]))
    row_b = next(r for r in rows if r.token_hash == hash_refresh_token(family_b["refresh_token"]))
    assert row_a.family_id != row_b.family_id  # two logins = two families
    # EVERY family-A token is dead — including A2, which was still fresh.
    rotated_row = next(
        r for r in rows if r.token_hash == hash_refresh_token(rotated["refresh_token"])
    )
    assert rotated_row.family_id == row_a.family_id
    family_a_rows = [r for r in rows if r.family_id == row_a.family_id]
    assert len(family_a_rows) == 2
    assert all(r.revoked_at is not None for r in family_a_rows)
    # Family B survived the nuke: B1 itself is revoked only because OUR
    # survivor call rotated it — the proof of survival is its rotation
    # child, which is live and carries family B's id.
    survivor_row = next(
        r for r in rows if r.token_hash == hash_refresh_token(survivor_token)
    )
    assert survivor_row.family_id == row_b.family_id
    assert survivor_row.revoked_at is None


async def test_login_mints_new_family_rotation_stays_in_it(
    client: AsyncClient, db: AsyncSession
) -> None:
    email = _email()
    await _register(client, email)
    first = (await _login(client, email)).json()
    second = (await _login(client, email)).json()

    row1 = (
        await db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == hash_refresh_token(first["refresh_token"])
            )
        )
    ).scalar_one()
    row2 = (
        await db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == hash_refresh_token(second["refresh_token"])
            )
        )
    ).scalar_one()
    assert row1.family_id != row2.family_id  # each login starts a new family

    refreshed = (
        await client.post(
            "/auth/refresh", json={"refresh_token": first["refresh_token"]}
        )
    ).json()
    row3 = (
        await db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == hash_refresh_token(refreshed["refresh_token"])
            )
        )
    ).scalar_one()
    assert row3.family_id == row1.family_id  # rotation stays inside the family


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
    # Human-readable body: wording via the shared formatter (handles the
    # 1-second singular) + machine-readable seconds field.
    body = last.json()["error"]
    assert body["details"]["retry_after_seconds"] == retry_after
    assert body["message"] == (
        f"Too many login attempts. You can try again in {format_retry_after(retry_after)}."
    )


async def test_register_rate_limited_429(client: AsyncClient) -> None:
    """Hammer ONE email: its (email, ip) bucket fills, then 429s. (Distinct
    emails get their own buckets — per the approved key design.)"""
    limit = get_settings().REGISTER_RATELIMIT_PER_MINUTE
    email = _email()
    last = None
    for _i in range(limit + 2):
        last = await _register(client, email)
    assert last is not None and last.status_code == 429
    assert "retry-after" in last.headers
    assert "registration attempts" in last.json()["error"]["message"]


async def test_refresh_rate_limited_429(client: AsyncClient) -> None:
    """Garbage-token hammering shares ONE ip-only sentinel bucket per IP."""
    limit = get_settings().REFRESH_RATELIMIT_PER_MINUTE
    last = None
    for _i in range(limit + 2):
        last = await client.post(
            "/auth/refresh", json={"refresh_token": "no-such-token"}
        )
    assert last is not None and last.status_code == 429
    assert last.json()["error"]["code"] == "rate_limited"
    assert "refresh attempts" in last.json()["error"]["message"]
    assert int(last.headers["retry-after"]) >= 1
    assert last.json()["error"]["details"]["retry_after_seconds"] == int(
        last.headers["retry-after"]
    )
