"""Phase 5 upload flow: idempotency replay, init/complete races, markup
rejection, owner charging, and the detail view urls.

Real Postgres + real SeaweedFS (same harness as test_files). Seeding
reuses the authz-matrix helpers (direct user/folder/grant inserts, tokens
minted in-process) so the shared-folder cases need no registration
round-trips.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db.models import File, IdempotencyKey, Outbox, PermissionRole
from app.repositories.quota import QuotaRepository
from tests.authz_matrix.conftest import (
    commit,
    headers_for,
    seed_folder,
    seed_grant,
    seed_user,
)
from tests.test_files import (
    JPEG_BYTES,
    PDF_BYTES,
    PNG_BYTES,
    TXT_BYTES,
    _init,
    _login,
    _upload,
)


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(get_settings().DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


# --- pending visibility ----------------------------------------------------------


async def test_pending_upload_visible_in_listing(client: AsyncClient) -> None:
    """User decision: init rows stay visible so clients can poll upload
    state straight from the listing."""
    headers = await _login(client)
    init = await _init(client, headers, "in-flight.bin", 100)
    assert init.status_code == 201, init.text
    file_id = init.json()["file_id"]

    listing = await client.get("/files", headers=headers)
    assert listing.status_code == 200
    item = next(i for i in listing.json()["items"] if i["id"] == file_id)
    assert item["status"] == "pending_upload"
    assert item["kind"] is None
    assert item["size_bytes"] == 100


# --- idempotency -------------------------------------------------------------------


async def test_idempotent_init_replays_stored_response(
    client: AsyncClient, db: AsyncSession
) -> None:
    headers = await _login(client)
    body = {"filename": "dup.bin", "content_type": "application/octet-stream", "size": 100}
    first = await client.post(
        "/files/init", headers={**headers, "Idempotency-Key": "K-replay"}, json=body
    )
    assert first.status_code == 201, first.text

    again = await client.post(
        "/files/init", headers={**headers, "Idempotency-Key": "K-replay"}, json=body
    )
    assert again.status_code == 201, again.text
    assert again.json() == first.json()  # verbatim: same id AND same url (not re-signed)

    rows = (
        (await db.execute(select(File).where(File.id == uuid.UUID(first.json()["file_id"]))))
        .scalars()
        .all()
    )
    assert len(rows) == 1  # no duplicate upload row
    stored = (
        (
            await db.execute(
                select(IdempotencyKey).where(
                    # Scoped to THIS run's user: rows persist between runs and
                    # the same header value is used by every past run too.
                    IdempotencyKey.user_id == rows[0].user_id,
                    IdempotencyKey.key == "K-replay",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(stored) == 1
    assert stored[0].response_status == 201
    assert stored[0].response_body == first.json()


async def test_idempotency_key_body_mismatch(client: AsyncClient) -> None:
    headers = await _login(client)
    body = {"filename": "plan.pdf", "content_type": "application/pdf", "size": 100}
    key = {"Idempotency-Key": "K-mismatch"}
    first = await client.post("/files/init", headers={**headers, **key}, json=body)
    assert first.status_code == 201, first.text

    for drift in ({**body, "size": 101}, {**body, "filename": "other.pdf"}):
        resp = await client.post("/files/init", headers={**headers, **key}, json=drift)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "idempotency_body_mismatch"


async def test_failed_init_leaves_no_idempotency_row(client: AsyncClient) -> None:
    """A rejected init must not consume the key: the failed attempt recorded
    nothing, so retrying the same key with a valid body executes fresh."""
    headers = await _login(client)
    key = {"Idempotency-Key": "K-fail"}
    bad = await client.post(
        "/files/init",
        headers={**headers, **key},
        json={
            "filename": "big.bin",
            "content_type": "application/octet-stream",
            "size": get_settings().max_upload_bytes + 1,
        },
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "file_too_large"

    good = await client.post(
        "/files/init",
        headers={**headers, **key},
        json={"filename": "ok.bin", "content_type": "application/octet-stream", "size": 10},
    )
    assert good.status_code == 201, good.text  # a leaked row would 422 here


async def test_parallel_same_key_inits_single_row(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Three concurrent inits, same key + body: the per-(user,key) advisory
    lock serializes them — one creates, the rest replay it."""
    headers = await _login(client)
    body = {"filename": "race.bin", "content_type": "application/octet-stream", "size": 50}
    key = {"Idempotency-Key": "K-race"}

    responses = await asyncio.gather(
        *(
            client.post("/files/init", headers={**headers, **key}, json=body)
            for _ in range(3)
        )
    )
    bodies = [r.json() for r in responses]
    assert all(r.status_code == 201 for r in responses), bodies
    assert len({b["file_id"] for b in bodies}) == 1, bodies

    rows = (
        (await db.execute(select(File).where(File.id == uuid.UUID(bodies[0]["file_id"]))))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    stored = (
        (
            await db.execute(
                select(IdempotencyKey).where(
                    IdempotencyKey.user_id == rows[0].user_id,
                    IdempotencyKey.key == "K-race",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(stored) == 1


# --- complete race ------------------------------------------------------------------


async def test_complete_race_exactly_one_outbox_row(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Two concurrent completes: the conditional UPDATE ... WHERE
    status='pending_upload' lets exactly one transition (202) — the loser
    reports 200 + current state, and only ONE file.uploaded row exists."""
    headers = await _login(client)
    init = await _init(client, headers, "race.pdf", len(PDF_BYTES))
    assert init.status_code == 201, init.text
    file_id = init.json()["file_id"]
    async with AsyncClient() as real:
        put = await real.put(init.json()["upload_url"], content=PDF_BYTES)
    assert put.status_code in (200, 201), f"S3 PUT failed: {put.status_code}"

    responses = await asyncio.gather(
        *(
            client.post(f"/files/{file_id}/complete", headers=headers)
            for _ in range(2)
        )
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 202], codes

    row = await db.get(File, uuid.UUID(file_id))
    assert row is not None
    assert row.status.value == "uploaded"
    assert row.sha256 == hashlib.sha256(PDF_BYTES).hexdigest()
    events = (
        (
            await db.execute(
                select(Outbox).where(
                    Outbox.event_type == "file.uploaded",
                    Outbox.payload["file_id"].astext == file_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1  # one winner announced it — duplicate deliveries impossible
    assert events[0].payload["size_bytes"] == len(PDF_BYTES)
    assert events[0].payload["sha256"] == row.sha256
    assert events[0].published_at is None  # the relay (Phase 6) publishes


# --- shared-folder charging ----------------------------------------------------------


async def test_editor_upload_charges_folder_owner(
    client: AsyncClient, db: AsyncSession
) -> None:
    """An editor uploading INTO someone else's folder charges the folder
    OWNER's quota and the row belongs to the owner (file.user_id = owner)."""
    owner = await seed_user(db)
    folder = await seed_folder(db, owner.id, name="shared")
    editor = await seed_user(db)
    await seed_grant(db, editor.id, PermissionRole.EDITOR, folder)
    await commit(db)

    editor_headers = headers_for(editor)
    init = await client.post(
        "/files/init",
        headers=editor_headers,
        json={
            "filename": "guest.jpg",
            "content_type": "application/octet-stream",
            "size": len(JPEG_BYTES),
            "folder_id": str(folder.id),
        },
    )
    assert init.status_code == 201, init.text
    file_id = init.json()["file_id"]
    async with AsyncClient() as real:
        put = await real.put(init.json()["upload_url"], content=JPEG_BYTES)
    assert put.status_code in (200, 201), f"S3 PUT failed: {put.status_code}"
    resp = await client.post(f"/files/{file_id}/complete", headers=editor_headers)
    assert resp.status_code == 202, resp.text

    row = await db.get(File, uuid.UUID(file_id))
    assert row is not None
    assert row.user_id == owner.id  # the folder OWNER owns the row

    quota = QuotaRepository(db)
    assert await quota.used_bytes(owner.id) == len(JPEG_BYTES)
    assert await quota.used_bytes(editor.id) == 0  # the uploader pays nothing

    event = (
        (
            await db.execute(
                select(Outbox).where(
                    Outbox.event_type == "file.uploaded",
                    Outbox.payload["file_id"].astext == file_id,
                )
            )
        )
        .scalar_one()
    )
    assert event.payload["owner_id"] == str(owner.id)
    assert event.payload["actor_id"] == str(editor.id)


async def test_viewer_init_is_404(client: AsyncClient, db: AsyncSession) -> None:
    """viewer < editor: placing an entry into the folder is a write → 404
    (denials are 404, never 403)."""
    owner = await seed_user(db)
    folder = await seed_folder(db, owner.id, name="readonly")
    viewer = await seed_user(db)
    await seed_grant(db, viewer.id, PermissionRole.VIEWER, folder)
    await commit(db)

    resp = await client.post(
        "/files/init",
        headers=headers_for(viewer),
        json={
            "filename": "sneaky.bin",
            "content_type": "application/octet-stream",
            "size": 10,
            "folder_id": str(folder.id),
        },
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


# --- markup rejection ----------------------------------------------------------------


async def test_svg_upload_rejected(client: AsyncClient, db: AsyncSession) -> None:
    """SVG (scriptable) must never enter the drive: 422 rejected_type, the
    object is killed, the reservation released, the row left pending with a
    zeroed size."""
    headers = await _login(client)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    init = await _init(client, headers, "diagram.svg", len(svg))
    assert init.status_code == 201, init.text
    file_id = init.json()["file_id"]
    async with AsyncClient() as real:
        put = await real.put(init.json()["upload_url"], content=svg)
    assert put.status_code in (200, 201), f"S3 PUT failed: {put.status_code}"

    resp = await client.post(f"/files/{file_id}/complete", headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "rejected_type"

    row = await db.get(File, uuid.UUID(file_id))
    assert row is not None
    assert row.status.value == "pending_upload"  # transition never happened
    assert row.size_bytes == 0  # zeroed: no double-release on later delete
    assert row.kind is None
    assert await QuotaRepository(db).used_bytes(row.user_id) == 0  # released

    detail = await client.get(f"/files/{file_id}", headers=headers)
    assert detail.status_code == 200
    async with AsyncClient() as real:
        fetch = await real.get(detail.json()["url"])
    assert fetch.status_code != 200  # the rejected object is gone from S3


@pytest.mark.parametrize(
    "payload",
    [
        b"<html><body>phishing page</body></html>",
        b"<!doctype html><html><body>x</body></html>",
        b'<?xml version="1.0"?><!DOCTYPE svg><svg onload="x()"/>',
    ],
)
async def test_markup_variants_rejected(client: AsyncClient, payload: bytes) -> None:
    headers = await _login(client)
    init = await _init(client, headers, "page.html", len(payload))
    assert init.status_code == 201, init.text
    async with AsyncClient() as real:
        put = await real.put(init.json()["upload_url"], content=payload)
    assert put.status_code in (200, 201), f"S3 PUT failed: {put.status_code}"
    resp = await client.post(f"/files/{init.json()['file_id']}/complete", headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "rejected_type"


# --- detail view urls ------------------------------------------------------------------


async def test_detail_embeds_url_roundtrip(client: AsyncClient) -> None:
    """GET /files/{id} now carries a 5-minute presigned GET; images serve
    inline and the round trip returns the exact bytes."""
    headers = await _login(client)
    uploaded = await _upload(client, headers, "pic.png", PNG_BYTES)
    assert uploaded.status_code == 202, uploaded.text
    file_id = uploaded.json()["id"]

    detail = await client.get(f"/files/{file_id}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["url"].startswith("http")
    assert body["url_expires_in"] == get_settings().FILE_URL_EXPIRE_SECONDS

    async with AsyncClient() as real:
        fetch = await real.get(body["url"])
    assert fetch.status_code == 200
    assert fetch.content == PNG_BYTES
    assert "attachment" not in body["url"]  # images render inline


async def test_detail_url_attachment_for_non_image(client: AsyncClient) -> None:
    headers = await _login(client)
    uploaded = await _upload(client, headers, "notes.txt", TXT_BYTES)
    assert uploaded.status_code == 202, uploaded.text
    file_id = uploaded.json()["id"]

    detail = await client.get(f"/files/{file_id}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    # Non-images are forced save-as, disposition carries the ORIGINAL name
    # (sanitized) — never the storage key, which leaks the owner id.
    assert "attachment" in body["url"]
    assert "notes.txt" in body["url"]


# --- delete of a pending row -----------------------------------------------------------


async def test_delete_pending_releases_reservation(
    client: AsyncClient, db: AsyncSession
) -> None:
    headers = await _login(client)
    init = await _init(client, headers, "abandoned.bin", 100)
    assert init.status_code == 201, init.text
    file_id = uuid.UUID(init.json()["file_id"])

    row = await db.get(File, file_id)
    assert row is not None
    owner_id = row.user_id
    assert await QuotaRepository(db).used_bytes(owner_id) == 100

    assert (await client.delete(f"/files/{file_id}", headers=headers)).status_code == 204
    assert await QuotaRepository(db).used_bytes(owner_id) == 0
    listing = await client.get("/files", headers=headers)
    assert all(i["id"] != str(file_id) for i in listing.json()["items"])
