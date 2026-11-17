"""Shared fixtures + seeds for the Phase 6/7 failure-injection suite.

Real Postgres + real SeaweedFS (same harness as every other suite — the dev
DB on :5433, bucket on :9000; users are hermetic via unique emails). The
celery BROKER is never started here: state-machine and sweep logic is driven
through the async internals, and the task body runs in a helper THREAD so
its internal asyncio.run() has no running loop to collide with pytest's.

Phase 7: `_drive_state_machine` now runs the REAL pipeline between the two
transitions, so tests that reach it must plant REAL bytes for the row's
kind (TINY_PNG decodes, TINY_PDF parses, EICAR_TEXT trips the scan stub).
Stale/no-op paths return before the pipeline and need no objects.
"""

from __future__ import annotations

import hashlib
import io
import threading
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import pytest
from sqlalchemy import delete, func, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db.models import File, FileKind, FileStatus, Outbox, User
from app.services.storage_service import StorageService
from app.workers.pipelines.scan import EICAR_SIGNATURE


@lru_cache(maxsize=1)
def tiny_png() -> bytes:
    """A REAL 1x1 PNG (Pillow-built once per session): header-parseable by
    the image pipeline's bomb guard, stub-mode labelable, tiny."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1, 1), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


@lru_cache(maxsize=1)
def tiny_pdf() -> bytes:
    """A REAL minimal PDF carrying extractable text (PyMuPDF-built)."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Quarterly finance report total 1250 EUR")
    data = doc.tobytes()
    doc.close()
    return data


# Scan-stub bait: plain text whose bytes contain the EICAR test string.
EICAR_TEXT = (EICAR_SIGNATURE + "\n").encode("ascii")
PLAIN_TEXT = b"plain text file, nothing to see here"


@pytest.fixture(autouse=True)
async def _clean_outbox(_pause_dev_pipeline: None) -> AsyncGenerator[None, None]:
    """Purge outbox rows AND aged stuck files before each FI test.

    The dev DB carries pytest residue from previous runs: unpublished outbox
    rows (the pipeline pause keeps the live relay from draining them
    mid-run) clog the relay's OLDEST-first claim batch, and uploaded/
    processing files older than STUCK_FILE_MINUTES get rescued by every
    sweep — each rescue writing another reprocess row. Both are pure test
    residue; deleting is safe while the pause is held (fixture dependency —
    nothing can be mid-claim, and no pytest test is running).
    """
    engine = create_async_engine(get_settings().DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        await session.execute(delete(Outbox))
        await session.execute(
            delete(File).where(
                File.status.in_([FileStatus.UPLOADED, FileStatus.PROCESSING]),
                File.created_at < func.now() - timedelta(minutes=10),
            )
        )
        await session.commit()
    yield
    await engine.dispose()


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(get_settings().DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


async def seed_user_with_quota(
    db: AsyncSession, *, quota_bytes: int = 10_000
) -> User:
    """One account with a small, explicit quota so release assertions are
    exact. Dummy password_hash — never logs in through the API."""
    user = User(
        email=f"fi-{uuid.uuid4().hex[:12]}@example.com",
        password_hash="x",
        storage_quota_bytes=quota_bytes,
        storage_used_bytes=0,
    )
    db.add(user)
    await db.flush()
    return user


async def seed_file(
    db: AsyncSession,
    user: User,
    *,
    status: FileStatus = FileStatus.UPLOADED,
    size_bytes: int | None = None,
    kind: FileKind | None = FileKind.IMAGE,
    upload_expires_at: datetime | None = None,
    plant_object: bool = False,
    data: bytes | None = None,
    name: str | None = None,
    mime: str | None = None,
) -> File:
    """One file row (and optionally the real storage object behind it).

    `data` plants REAL bytes and mirrors the /complete flow: sha256 is
    stored (the pipeline's integrity check runs against it) and size
    defaults to len(data). Without data the legacy JPEG-magic stub bytes
    keep old tests byte-identical. `name`/`mime` matter to the pipelines
    only via the row's `kind` (dispatch) — they label the row for the
    listing."""
    file_name = name or "fi-file.jpg"
    fid = uuid.uuid4()
    suffix = file_name.rsplit(".", 1)[-1]
    storage_key = f"{user.id}/{fid}.{suffix}"
    if size_bytes is None:
        size_bytes = len(data) if data is not None else 100
    file = File(
        id=fid,
        user_id=user.id,
        original_name=file_name,
        mime_type=mime or "image/jpeg",
        size_bytes=size_bytes,
        storage_key=storage_key,
        status=status,
        kind=kind,
        upload_expires_at=upload_expires_at,
        sha256=hashlib.sha256(data).hexdigest() if data is not None else None,
    )
    db.add(file)
    await db.flush()
    if plant_object:
        storage = StorageService()
        await storage.start()
        try:
            body = (
                data
                if data is not None
                else b"\xff\xd8\xff\xe0" + b"\x00" * max(size_bytes - 4, 0)
            )
            await storage.put_bytes(storage_key, body)
        finally:
            await storage.close()
    return file


async def seed_outbox_event(
    db: AsyncSession,
    *,
    event_type: str = "file.uploaded",
    file_id: uuid.UUID | None = None,
    published: bool = False,
    payload: dict[str, Any] | None = None,
) -> Outbox:
    row = Outbox(
        event_type=event_type,
        payload=payload if payload is not None else {"file_id": str(file_id or uuid.uuid4())},
    )
    if published:
        row.published_at = datetime.now(UTC)
    db.add(row)
    await db.flush()
    return row


async def age_created_at(db: AsyncSession, file_id: uuid.UUID, minutes: float) -> None:
    """Make a file look stuck: DB-side backdated created_at (injection)."""
    await db.execute(
        update(File)
        .where(File.id == file_id)
        .values(created_at=func.now() - timedelta(minutes=minutes))
    )
    await db.commit()


async def age_upload_expiry(db: AsyncSession, file_id: uuid.UUID, seconds: float) -> None:
    """Make a pending upload look abandoned: backdated upload_expires_at."""
    await db.execute(
        update(File)
        .where(File.id == file_id)
        .values(upload_expires_at=func.now() - timedelta(seconds=seconds))
    )
    await db.commit()


class FakeRequest:
    def __init__(self, retries: int) -> None:
        self.retries = retries


class FakeTask:
    """The _TaskLike slice of celery.Task: records retry calls, raises the
    real celery Retry so `pytest.raises(Retry)` mirrors worker behavior."""

    def __init__(self, *, retries: int = 0, max_retries: int = 5) -> None:
        self.max_retries = max_retries
        self.request = FakeRequest(retries)
        self.retry_calls: list[float] = []

    def retry(self, *, exc: BaseException, countdown: float) -> Any:
        from celery.exceptions import Retry

        self.retry_calls.append(countdown)
        raise Retry()


class FakeBroker:
    """Records send_task calls; optional permanent failure injection.
    `sent` stays (task, file_id) for the file-event assertions;
    `sent_kwargs` records the full kwargs (storage.delete carries a
    storage_key, not a file_id)."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.sent_kwargs: list[tuple[str, dict[str, str]]] = []
        self.fail = fail

    def send_task(self, name: str, kwargs: dict[str, str] | None = None) -> None:
        if self.fail is not None:
            raise self.fail
        self.sent_kwargs.append((name, dict(kwargs or {})))
        self.sent.append((name, (kwargs or {}).get("file_id", "")))


def make_factory() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    """A fresh engine + session factory on its own connection — races and
    relay/sweeper invocations must never share the seeding session's
    connection or identity map."""
    engine = create_async_engine(get_settings().DATABASE_URL, poolclass=NullPool)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def run_task_body(task: Any, file_id: str) -> None:
    """Run the task body the way a real worker does — in a SYNC context with
    its own event loop (asyncio.run cannot run inside pytest's loop). The
    thread also proves the body owns its failure handling: whatever escapes
    is re-raised on the test thread."""
    caught: list[BaseException] = []

    def target() -> None:
        try:
            from app.workers import tasks as tasks_module

            tasks_module._process_file_body(task, file_id)
        except BaseException as exc:
            caught.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=60)
    assert not thread.is_alive(), "task body thread hung"
    if caught:
        raise caught[0]
