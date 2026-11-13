"""P3 files integration tests — real SeaweedFS + real Postgres.

Full upload lifecycle per test: init (API) → PUT bytes (httpx → presigned
SeaweedFS url) → complete (API: HEAD + stream-hash + sniff). Unique emails
keep users hermetic across runs; unique byte payloads per upload keep dedup
assertions deterministic.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db.models import File

# --- deterministic sample bytes -------------------------------------------------
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 32
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WEBP_BYTES = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24
PDF_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"1 0 obj\nendobj\n" + b"\x00" * 16
DOCX_BYTES = b"PK\x03\x04[Content_Types].xml" + b"\x00" * 32
TXT_BYTES = b"hello world\nthis is a text file\n"
CSV_BYTES = b"a,b,c\n1,2,3\n"
BAT_BYTES = b"@echo off\r\necho pwned\r\n"
FAKE_JPG_BYTES = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 32  # an .exe named .jpg


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(get_settings().DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def _email() -> str:
    return f"files-{uuid.uuid4().hex[:10]}@example.com"


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client: AsyncClient) -> dict[str, str]:
    email = _email()
    resp = await client.post(
        "/auth/register", json={"email": email, "password": "correct-horse-1"}
    )
    assert resp.status_code == 201, resp.text
    resp = await client.post(
        "/auth/login", json={"email": email, "password": "correct-horse-1"}
    )
    assert resp.status_code == 200, resp.text
    return _auth_header(resp.json()["access_token"])


async def _init(
    client: AsyncClient, headers: dict[str, str], filename: str, size: int
) -> Response:
    return await client.post(
        "/files/init",
        headers=headers,
        json={"filename": filename, "content_type": "application/octet-stream", "size": size},
    )


async def _upload(
    client: AsyncClient,
    headers: dict[str, str],
    filename: str,
    data: bytes,
) -> Response:
    """Full happy-path upload: init → PUT to presigned url → complete.

    The S3 PUT must use a REAL network client: the test `client` uses
    ASGITransport, which routes every request — even absolute URLs — into
    the app itself. A browser/curl would reach SeaweedFS; only the test
    harness needs this escape hatch.
    """
    init = await _init(client, headers, filename, len(data))
    assert init.status_code == 201, init.text
    async with AsyncClient() as real:
        put = await real.put(init.json()["upload_url"], content=data)
    assert put.status_code in (200, 201), f"S3 PUT failed: {put.status_code} {put.text[:120]}"
    return await client.post(f"/files/{init.json()['file_id']}/complete", headers=headers)


# --- init ---------------------------------------------------------------------


async def test_init_creates_row_and_presigns(client: AsyncClient, db: AsyncSession) -> None:
    headers = await _login(client)
    resp = await _init(client, headers, "photo.jpg", 100)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert uuid.UUID(body["file_id"])
    assert body["expires_in"] == get_settings().PRESIGNED_URL_EXPIRE_SECONDS
    assert "localhost:9000" in body["upload_url"]

    row = await db.get(File, uuid.UUID(body["file_id"]))
    assert row is not None
    assert row.status.value == "uploaded"
    assert row.kind is None
    assert row.sha256 is None  # NULL until complete hashes the bytes
    assert row.storage_key.endswith(".jpg")


async def test_init_rejects_oversize(client: AsyncClient) -> None:
    headers = await _login(client)
    too_big = get_settings().max_upload_bytes + 1
    resp = await _init(client, headers, "big.bin", too_big)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "file_too_large"


async def test_init_rejects_nonpositive_size(client: AsyncClient) -> None:
    headers = await _login(client)
    resp = await _init(client, headers, "zero.bin", 0)
    assert resp.status_code == 422


# --- complete: kind classification --------------------------------------------


async def test_complete_image_kinds(client: AsyncClient, db: AsyncSession) -> None:
    headers = await _login(client)
    for filename, data, expected_mime in (
        ("photo.jpg", JPEG_BYTES, "image/jpeg"),
        ("shot.png", PNG_BYTES, "image/png"),
        ("anim.webp", WEBP_BYTES, "image/webp"),
    ):
        resp = await _upload(client, headers, filename, data)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["kind"] == "image"
        assert body["mime_type"] == expected_mime
        assert body["status"] == "uploaded"  # P4 worker will process later
        assert body["deduplicated"] is False
        assert body["sha256"] == hashlib.sha256(data).hexdigest()
        assert body["size_bytes"] == len(data)


async def test_complete_pdf_and_office(client: AsyncClient) -> None:
    headers = await _login(client)
    pdf = await _upload(client, headers, "report.pdf", PDF_BYTES)
    assert pdf.status_code == 200
    assert pdf.json()["kind"] == "pdf"
    assert pdf.json()["mime_type"] == "application/pdf"

    docx = await _upload(client, headers, "notes.docx", DOCX_BYTES)
    assert docx.status_code == 200, docx.text
    assert docx.json()["kind"] == "document"
    assert (
        docx.json()["mime_type"]
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


async def test_complete_text_kinds(client: AsyncClient) -> None:
    headers = await _login(client)
    txt = await _upload(client, headers, "readme.txt", TXT_BYTES)
    assert txt.status_code == 200
    assert txt.json()["kind"] == "text"
    assert txt.json()["mime_type"] == "text/plain"

    csv = await _upload(client, headers, "data.csv", CSV_BYTES)
    assert csv.json()["kind"] == "text"
    assert csv.json()["mime_type"] == "text/csv"


async def test_complete_other_binary_is_done_and_browsable(
    client: AsyncClient,
) -> None:
    """Matrix A: .bat → kind=other, doc_topic=other, status=done."""
    headers = await _login(client)
    resp = await _upload(client, headers, "setup.bat", BAT_BYTES)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "other"
    assert body["doc_topic"] == "other"
    assert body["status"] == "done"


async def test_sniff_wins_fake_jpg(client: AsyncClient) -> None:
    """An .exe named .jpg must NOT enter the image pipeline (user decision)."""
    headers = await _login(client)
    resp = await _upload(client, headers, "trojan.jpg", FAKE_JPG_BYTES)
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "other"
    assert body["mime_type"] == "application/octet-stream"
    assert body["doc_topic"] == "other"
    assert body["status"] == "done"


# --- complete: failure modes ----------------------------------------------------


async def test_complete_missing_object_409(client: AsyncClient) -> None:
    headers = await _login(client)
    init = await _init(client, headers, "never-put.bin", 10)
    resp = await client.post(
        f"/files/{init.json()['file_id']}/complete", headers=headers
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "upload_not_found"


async def test_complete_is_idempotent(client: AsyncClient) -> None:
    headers = await _login(client)
    first = await _upload(client, headers, "doc.pdf", PDF_BYTES)
    assert first.status_code == 200
    file_id = first.json()["id"]
    again = await client.post(f"/files/{file_id}/complete", headers=headers)
    assert again.status_code == 200
    assert again.json()["id"] == file_id
    assert again.json()["deduplicated"] is False
    assert again.json()["sha256"] == first.json()["sha256"]


async def test_dedup_returns_existing_row(client: AsyncClient, db: AsyncSession) -> None:
    headers = await _login(client)
    # Unique-per-run bytes: DB rows persist across test runs, and the count
    # query below has no user filter — a fixed payload would also count rows
    # left by earlier runs. Appended bytes keep the JPEG magic intact.
    data = JPEG_BYTES + uuid.uuid4().bytes
    first = await _upload(client, headers, "a.jpg", data)
    second = await _upload(client, headers, "b.jpg", data)  # same bytes!
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["deduplicated"] is True
    assert body["id"] == first.json()["id"]  # original row wins
    assert body["original_name"] == "a.jpg"

    rows = (
        await db.execute(
            select(File).where(File.sha256 == hashlib.sha256(data).hexdigest())
        )
    ).scalars().all()
    assert len(rows) == 1  # duplicate row deleted, object never stored twice


# --- browse ----------------------------------------------------------------------


async def test_pagination_newest_first_with_cursor(client: AsyncClient) -> None:
    headers = await _login(client)
    # Three distinct payloads → three rows, ordered by created_at (newest last
    # uploaded = first in list).
    payloads = [b"payload-one\x01", b"payload-two\x02", b"payload-three\x03"]
    ids = []
    for i, data in enumerate(payloads):
        resp = await _upload(client, headers, f"p{i}.txt", data)
        assert resp.status_code == 200
        ids.append(resp.json()["id"])

    page1 = await client.get("/files", headers=headers, params={"limit": 2})
    assert page1.status_code == 200
    body = page1.json()
    assert [i["id"] for i in body["items"]] == [ids[2], ids[1]]  # newest first
    assert body["next_cursor"]

    page2 = await client.get(
        "/files", headers=headers, params={"limit": 2, "cursor": body["next_cursor"]}
    )
    body2 = page2.json()
    assert [i["id"] for i in body2["items"]] == [ids[0]]
    assert body2["next_cursor"] is None


async def test_list_filters(client: AsyncClient) -> None:
    headers = await _login(client)
    await _upload(client, headers, "img.jpg", JPEG_BYTES)
    await _upload(client, headers, "tool.bat", BAT_BYTES)

    images = await client.get("/files", headers=headers, params={"kind": "image"})
    assert images.status_code == 200
    assert all(i["kind"] == "image" for i in images.json()["items"])
    assert len(images.json()["items"]) >= 1

    others = await client.get("/files", headers=headers, params={"doc_topic": "other"})
    assert all(i["doc_topic"] == "other" for i in others.json()["items"])
    assert all(i["status"] == "done" for i in others.json()["items"])

    bad = await client.get("/files", headers=headers, params={"kind": "nope"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "bad_filter"


async def test_bad_cursor_422(client: AsyncClient) -> None:
    headers = await _login(client)
    resp = await client.get("/files", headers=headers, params={"cursor": "!!!garbage"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "bad_cursor"


# --- ownership (404-not-403) ------------------------------------------------------


async def test_cross_user_access_is_404(client: AsyncClient) -> None:
    owner_headers = await _login(client)
    uploaded = await _upload(client, owner_headers, "secret.jpg", JPEG_BYTES)
    file_id = uploaded.json()["id"]

    intruder_headers = await _login(client)
    for method, path, kwargs in (
        ("get", f"/files/{file_id}", {}),
        ("post", f"/files/{file_id}/complete", {}),
        ("patch", f"/files/{file_id}", {"json": {"original_name": "steal.jpg"}}),
        ("delete", f"/files/{file_id}", {}),
        ("get", f"/files/{file_id}/url", {}),
    ):
        resp = await getattr(client, method)(path, headers=intruder_headers, **kwargs)
        assert resp.status_code == 404, f"{method} {path} → {resp.status_code}"

    # Owner still intact.
    mine = await client.get(f"/files/{file_id}", headers=owner_headers)
    assert mine.status_code == 200


# --- metadata / url / rename / delete ----------------------------------------------


async def test_view_url_endpoint(client: AsyncClient) -> None:
    headers = await _login(client)
    uploaded = await _upload(client, headers, "pic.png", PNG_BYTES)
    file_id = uploaded.json()["id"]

    resp = await client.get(f"/files/{file_id}/url", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["url"].startswith("http")
    assert body["expires_in"] == get_settings().PRESIGNED_URL_EXPIRE_SECONDS

    dl = await client.get(f"/files/{file_id}/url", headers=headers, params={"download": "true"})
    assert dl.status_code == 200


async def test_rename(client: AsyncClient, db: AsyncSession) -> None:
    headers = await _login(client)
    uploaded = await _upload(client, headers, "old name.jpg", JPEG_BYTES)
    file_id = uploaded.json()["id"]

    resp = await client.patch(
        f"/files/{file_id}", headers=headers, json={"original_name": "vacation 2026.jpg"}
    )
    assert resp.status_code == 200
    assert resp.json()["original_name"] == "vacation 2026.jpg"

    row = await db.get(File, uuid.UUID(file_id))
    assert row is not None and row.original_name == "vacation 2026.jpg"


async def test_delete_removes_row_and_object(client: AsyncClient) -> None:
    headers = await _login(client)
    uploaded = await _upload(client, headers, "doomed.txt", TXT_BYTES)
    file_id = uploaded.json()["id"]
    url_resp = await client.get(f"/files/{file_id}/url", headers=headers)
    object_url = url_resp.json()["url"]

    resp = await client.delete(f"/files/{file_id}", headers=headers)
    assert resp.status_code == 204

    assert (await client.get(f"/files/{file_id}", headers=headers)).status_code == 404
    listing = await client.get("/files", headers=headers)
    assert all(i["id"] != file_id for i in listing.json()["items"])

    # The stored object itself is gone from S3 (presigned GET must not 200).
    async with AsyncClient() as real:
        fetch = await real.get(object_url)
    assert fetch.status_code != 200


# --- presign endpoint (public vs internal) --------------------------------------


async def test_presigned_urls_use_public_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Handed-out URLs must target the PUBLIC endpoint, and still work.

    In containers the API signs against s3:8333 but clients can only reach
    localhost:9000 — the presign client must be bound to the public host.
    The PUT below proves the signature is valid for the public host too.
    """
    from app.core.config import get_settings
    from app.services.storage import StorageService

    monkeypatch.setenv("S3_PUBLIC_ENDPOINT_URL", "http://127.0.0.1:9000")
    get_settings.cache_clear()
    storage = StorageService()
    await storage.start()
    try:
        put_url, _ = await storage.presign_put("presign-check/x", 4)
        assert "127.0.0.1:9000" in put_url  # public host, not the internal one
        get_url, _ = await storage.presign_get("presign-check/x", download=False)
        assert "127.0.0.1:9000" in get_url
        async with AsyncClient() as real:
            put = await real.put(put_url, content=b"abcd")
        assert put.status_code in (200, 201), f"public-endpoint PUT failed: {put.status_code}"
    finally:
        await storage.close()


# --- auth guard ----------------------------------------------------------------


async def test_files_require_auth(client: AsyncClient) -> None:
    assert (await client.get("/files")).status_code == 401
    assert (
        await client.post(
            "/files/init", json={"filename": "x.jpg", "content_type": "image/jpeg", "size": 1}
        )
    ).status_code == 401
