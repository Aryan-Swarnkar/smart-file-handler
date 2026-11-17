"""Phase 7 pipeline tests: scan -> quarantine, kind pipelines, mapping.

Same harness as the failure-injection suite (the `db` fixture + autouse
pause/outbox hygiene live here): runs against the live dev DB/bucket with
the pause lock held, driving `_drive_state_machine` / task bodies directly.
Task bodies run in helper threads so their asyncio.run has no loop to
collide with pytest's.

ClamAV runs in STUB mode here (ML_STUB=1): infected iff EICAR bytes. The
REAL clamd path (worker -> clamd inside the compose network) is proven by
backups/live_e2e.py's Phase 7 section.
"""

from __future__ import annotations

import struct
import threading
import uuid
import zlib

import pytest
from celery.exceptions import Retry
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import File, FileKind, FileLabel, FileStatus, Label, LabelKind, Outbox
from app.repositories.files import FileRepository
from app.repositories.quota import QuotaRepository
from app.services.file_service import FileService
from app.services.storage_service import StorageService
from app.workers import relay as relay_module
from app.workers import tasks as tasks_module
from app.workers.pipelines.models_registry import DOC_TOPICS, IMAGE_CONCEPTS
from app.workers.pipelines.scan import ScanUnavailable
from tests.failure_injection.conftest import (
    EICAR_TEXT,
    PLAIN_TEXT,
    FakeBroker,
    FakeTask,
    make_factory,
    run_task_body,
    seed_file,
    seed_outbox_event,
    seed_user_with_quota,
    tiny_pdf,
    tiny_png,
)


async def status_of(db: AsyncSession, file_id: uuid.UUID) -> FileStatus | None:
    return await FileRepository(db).get_status(file_id)


async def error_of(db: AsyncSession, file_id: uuid.UUID) -> str | None:
    return (
        await db.execute(select(File.error).where(File.id == file_id))
    ).scalar_one()


async def used_bytes(db: AsyncSession, user_id: uuid.UUID) -> int | None:
    return await QuotaRepository(db).used_bytes(user_id)


async def storage_delete_rows(db: AsyncSession, storage_key: str) -> list[Outbox]:
    return (
        await db.execute(
            select(Outbox).where(
                Outbox.event_type == "storage.delete",
                Outbox.payload["storage_key"].astext == storage_key,
            )
        )
    ).scalars().all()


def bomb_png(width: int = 60_000, height: int = 60_000) -> bytes:
    """A VALID PNG header claiming huge dimensions (CRC-correct IHDR): the
    decompression-bomb shape — tiny bytes, catastrophic declared raster."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00" * 64))
        + chunk(b"IEND", b"")
    )


def run_delete_task(storage_key: str) -> None:
    """delete_object's body asyncio.run()s — drive it in a thread."""
    caught: list[BaseException] = []

    def target() -> None:
        try:
            tasks_module.delete_object(storage_key)
        except BaseException as exc:
            caught.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=60)
    assert not thread.is_alive(), "delete_object thread hung"
    if caught:
        raise caught[0]


# --- quarantine path (scan stub: infected iff EICAR bytes) -----------------------
# NOTE: the host-level tests NEVER write real EICAR bytes to disk — Windows
# Defender's real-time scan quarantines such temp files mid-test (detection
# history proved it; the read then fails with a confusing EINVAL). Split:
# the stub DETECTOR is unit-tested as a pure function, the quarantine
# MECHANICS run against a monkeypatched infected verdict, and real EICAR
# through real clamd is proven in-container by backups/live_e2e.py.


def test_stub_scan_detects_eicar_in_bytes() -> None:
    from app.workers.pipelines import scan as scan_module

    verdict = scan_module._stub_verdict(EICAR_TEXT)
    assert verdict.infected is True
    assert verdict.signature == "Eicar-Test-Signature"
    assert scan_module._stub_verdict(PLAIN_TEXT).infected is False


async def test_infected_verdict_quarantined(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await seed_user_with_quota(db)
    assert await QuotaRepository(db).reserve(user.id, len(PLAIN_TEXT))
    file = await seed_file(
        db,
        user,
        status=FileStatus.UPLOADED,
        kind=FileKind.OTHER,
        name="invoice.txt",
        mime="text/plain",
        plant_object=True,
        data=PLAIN_TEXT,
    )
    await db.commit()

    from app.workers.pipelines.scan import ScanVerdict

    async def infected(path: object) -> ScanVerdict:
        return ScanVerdict(infected=True, signature="Eicar-Test-Signature")

    monkeypatch.setattr(tasks_module, "scan_file", infected)
    await tasks_module._drive_state_machine(file.id)

    assert await status_of(db, file.id) is FileStatus.QUARANTINED
    # Quota released in the SAME transaction as the status + outbox write.
    assert await used_bytes(db, user.id) == 0
    # The object removal rides the outbox (the relay is the only publisher).
    rows = await storage_delete_rows(db, file.storage_key)
    assert len(rows) == 1
    assert rows[0].published_at is None
    # Quarantined is its own terminal state — not `failed`, no error text.
    assert await error_of(db, file.id) is None


async def test_quarantined_survives_redelivery(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(
        db,
        user,
        status=FileStatus.UPLOADED,
        kind=FileKind.OTHER,
        name="e.txt",
        mime="text/plain",
        plant_object=True,
        data=PLAIN_TEXT,
    )
    await db.commit()

    from app.workers.pipelines.scan import ScanVerdict

    async def infected(path: object) -> ScanVerdict:
        return ScanVerdict(infected=True, signature="Eicar-Test-Signature")

    monkeypatch.setattr(tasks_module, "scan_file", infected)
    await tasks_module._drive_state_machine(file.id)
    await tasks_module._drive_state_machine(file.id)  # duplicate delivery

    assert await status_of(db, file.id) is FileStatus.QUARANTINED
    assert await used_bytes(db, user.id) == 0  # release is guarded, not doubled
    assert len(await storage_delete_rows(db, file.storage_key)) == 1


async def test_quarantine_lost_race_writes_nothing(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    assert await QuotaRepository(db).reserve(user.id, 100)
    file = await seed_file(db, user, status=FileStatus.UPLOADED)
    await db.commit()

    engine, factory = make_factory()
    try:
        # The row is UPLOADED, not PROCESSING: the conditional UPDATE loses,
        # so quota + outbox must be untouched (the winner owns the outcome).
        await tasks_module._quarantine(
            factory,
            file.id,
            user_id=user.id,
            size_bytes=100,
            storage_key=file.storage_key,
        )
    finally:
        await engine.dispose()

    assert await status_of(db, file.id) is FileStatus.UPLOADED
    assert await used_bytes(db, user.id) == 100
    assert await storage_delete_rows(db, file.storage_key) == []


# --- scan unavailability + size guard ---------------------------------------------


async def test_scan_unavailable_retries_never_marks_clean(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(
        db, user, status=FileStatus.UPLOADED, plant_object=True, data=tiny_png()
    )
    await db.commit()

    async def clamd_down(path: object) -> object:
        raise ScanUnavailable("clamd unreachable (ConnectionRefusedError)")

    monkeypatch.setattr(tasks_module, "scan_file", clamd_down)
    with pytest.raises(Retry):  # ScanUnavailable -> RETRYABLE, always
        run_task_body(FakeTask(retries=0, max_retries=5), str(file.id))

    assert await status_of(db, file.id) is FileStatus.PROCESSING
    assert await error_of(db, file.id) is None  # never marked clean OR failed


async def test_oversized_object_fails_before_scan(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    # Declared size past the clamd stream limit; the object itself is tiny
    # (the guard runs on the row's declared size, before any scan stream).
    file = await seed_file(
        db,
        user,
        status=FileStatus.UPLOADED,
        plant_object=True,
        data=tiny_png(),
        size_bytes=51 * 1024 * 1024,
    )
    await db.commit()

    run_task_body(FakeTask(retries=0, max_retries=5), str(file.id))  # no raise

    assert await status_of(db, file.id) is FileStatus.FAILED
    error = await error_of(db, file.id)
    assert error is not None and "scan:" in error and "stream limit" in error


# --- image pipeline ----------------------------------------------------------------


async def test_bomb_png_rejected_before_decode(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    bomb = bomb_png()
    file = await seed_file(
        db, user, status=FileStatus.UPLOADED, plant_object=True, data=bomb
    )
    await db.commit()

    run_task_body(FakeTask(retries=0, max_retries=5), str(file.id))  # no raise

    assert await status_of(db, file.id) is FileStatus.FAILED
    error = await error_of(db, file.id)
    assert error is not None and error.startswith("format:")
    assert "processing limit" in error


async def test_stub_image_labels_written(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(
        db, user, status=FileStatus.UPLOADED, plant_object=True, data=tiny_png()
    )
    await db.commit()

    await tasks_module._drive_state_machine(file.id)
    assert await status_of(db, file.id) is FileStatus.DONE

    labels = (
        await db.execute(
            select(Label.name, FileLabel.confidence)
            .join(Label, Label.id == FileLabel.label_id)
            .where(FileLabel.file_id == file.id, Label.kind == LabelKind.THING)
        )
    ).all()
    assert 2 <= len(labels) <= 4  # the stub's pick size
    assert all(name in IMAGE_CONCEPTS for name, _ in labels)
    assert all(conf >= 0.75 for _, conf in labels)
    # Labels are PER-USER: they hang off the owner's label rows.
    owner_ids = (
        await db.execute(
            select(Label.user_id)
            .join(FileLabel, FileLabel.label_id == Label.id)
            .where(FileLabel.file_id == file.id)
        )
    ).scalars().all()
    assert set(owner_ids) == {user.id}


async def test_stub_image_reprocessing_is_idempotent(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(
        db, user, status=FileStatus.UPLOADED, plant_object=True, data=tiny_png()
    )
    await db.commit()

    await tasks_module._drive_state_machine(file.id)

    async def label_rows() -> list[tuple[str, float]]:
        return (
            await db.execute(
                select(Label.name, FileLabel.confidence)
                .join(Label, Label.id == FileLabel.label_id)
                .where(FileLabel.file_id == file.id)
                .order_by(Label.name)
            )
        ).all()

    first = await label_rows()
    await tasks_module._drive_state_machine(file.id)  # rescue/duplicate
    assert await label_rows() == first  # replaced, never duplicated


# --- pdf pipeline --------------------------------------------------------------------


async def test_stub_pdf_text_topic_and_label(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(
        db,
        user,
        status=FileStatus.UPLOADED,
        kind=FileKind.PDF,
        name="report.pdf",
        mime="application/pdf",
        plant_object=True,
        data=tiny_pdf(),
    )
    await db.commit()

    await tasks_module._drive_state_machine(file.id)
    assert await status_of(db, file.id) is FileStatus.DONE

    # get_fresh, not db.get: the identity map still holds the seeded entity
    # (the pipeline wrote in its own engine/session — see FileRepository).
    fresh = await FileRepository(db).get_fresh(file.id)
    assert fresh is not None
    assert fresh.extracted_text is not None
    assert "Quarterly finance report" in fresh.extracted_text
    assert fresh.doc_topic in DOC_TOPICS
    # The generated tsvector picked the text up in the same row write.
    assert fresh.search_vector is not None
    # Exactly one DOC_TOPIC label, matching the denormalized topic.
    topics = (
        await db.execute(
            select(Label.name)
            .join(FileLabel, FileLabel.label_id == Label.id)
            .where(FileLabel.file_id == file.id, Label.kind == LabelKind.DOC_TOPIC)
        )
    ).scalars().all()
    assert topics == [fresh.doc_topic]


async def test_corrupt_pdf_fails_without_retry_loop(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(
        db,
        user,
        status=FileStatus.UPLOADED,
        kind=FileKind.PDF,
        name="broken.pdf",
        mime="application/pdf",
        plant_object=True,
        data=b"this is not a pdf at all" * 10,
    )
    await db.commit()

    run_task_body(FakeTask(retries=0, max_retries=5), str(file.id))  # no raise

    assert await status_of(db, file.id) is FileStatus.FAILED
    error = await error_of(db, file.id)
    assert error == "format: unreadable pdf"


# --- scan-only kinds + integrity ------------------------------------------------------


async def test_other_kind_is_scan_only(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(
        db,
        user,
        status=FileStatus.UPLOADED,
        kind=FileKind.OTHER,
        name="notes.txt",
        mime="text/plain",
        plant_object=True,
        data=PLAIN_TEXT,
    )
    await db.commit()

    await tasks_module._drive_state_machine(file.id)
    assert await status_of(db, file.id) is FileStatus.DONE

    count = (
        await db.execute(
            select(func.count())
            .select_from(FileLabel)
            .where(FileLabel.file_id == file.id)
        )
    ).scalar_one()
    assert count == 0  # no labels for other kinds
    fresh = await FileRepository(db).get_fresh(file.id)  # identity-map-safe
    assert fresh is not None
    assert fresh.doc_topic is None and fresh.extracted_text is None


async def test_hash_mismatch_fails_deterministically(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(
        db, user, status=FileStatus.UPLOADED, plant_object=True, data=tiny_png()
    )
    await db.commit()

    # Tamper AFTER the row (and its sha256) exists: bytes changed under us.
    storage = StorageService()
    await storage.start()
    try:
        await storage.put_bytes(file.storage_key, b"tampered bytes")
    finally:
        await storage.close()

    run_task_body(FakeTask(retries=0, max_retries=5), str(file.id))  # no raise

    assert await status_of(db, file.id) is FileStatus.FAILED
    assert await error_of(db, file.id) == "integrity: sha256 mismatch"


async def test_missing_object_fails_deterministically(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(db, user, status=FileStatus.UPLOADED)  # no object
    await db.commit()

    run_task_body(FakeTask(retries=0, max_retries=5), str(file.id))  # no raise

    assert await status_of(db, file.id) is FileStatus.FAILED
    assert await error_of(db, file.id) == "storage: object missing"


# --- quarantined visibility ----------------------------------------------------------


async def test_quarantined_detail_404_but_listed(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(db, user, status=FileStatus.QUARANTINED)
    await db.commit()

    service = FileService(db, StorageService())  # storage unused on reads
    with pytest.raises(NotFoundError):
        await service.get_file(user.id, file.id)

    page = await service.list_files(user.id, limit=10, cursor=None)
    assert [f.id for f in page.items] == [file.id]  # listed: the user must
    assert page.items[0].status is FileStatus.QUARANTINED  # see WHY quota fell


async def test_deleting_quarantined_file_never_double_releases(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quarantine already released the reservation — the owner's delete of
    the husk (still listed) must not release AGAIN (free-space drift)."""
    user = await seed_user_with_quota(db)
    assert await QuotaRepository(db).reserve(user.id, len(PLAIN_TEXT))
    file = await seed_file(
        db,
        user,
        status=FileStatus.UPLOADED,
        kind=FileKind.OTHER,
        name="q.txt",
        mime="text/plain",
        plant_object=True,
        data=PLAIN_TEXT,
    )
    await db.commit()

    from app.workers.pipelines.scan import ScanVerdict

    async def infected(path: object) -> ScanVerdict:
        return ScanVerdict(infected=True, signature="Eicar-Test-Signature")

    monkeypatch.setattr(tasks_module, "scan_file", infected)
    await tasks_module._drive_state_machine(file.id)
    assert await used_bytes(db, user.id) == 0  # quarantine released

    storage = StorageService()
    await storage.start()
    try:
        service = FileService(db, storage)
        await service.delete_file(user.id, file.id)
        assert await used_bytes(db, user.id) == 0  # no second release
        assert await status_of(db, file.id) is None  # row gone
    finally:
        await db.rollback()  # discard the session's identity map
        await storage.close()


# --- file deletion cascades (found by the live E2E) --------------------------------


async def test_delete_labeled_file_cascades_associations(db: AsyncSession) -> None:
    """Regression: deleting a file WITH file_labels used to raise
    "tried to blank-out primary key column 'file_labels.file_id'" — the
    one-to-many default (SET NULL the child FK) is illegal for a PK member.
    With passive_deletes the DB's ON DELETE CASCADE removes the association
    rows; the per-user Label rows survive (they are reusable)."""
    user = await seed_user_with_quota(db)
    assert await QuotaRepository(db).reserve(user.id, len(PLAIN_TEXT))
    file = await seed_file(
        db,
        user,
        status=FileStatus.DONE,
        kind=FileKind.OTHER,
        name="labeled.txt",
        mime="text/plain",
        data=PLAIN_TEXT,
    )
    label = Label(user_id=user.id, name="document", kind=LabelKind.THING)
    db.add(label)
    await db.flush()
    db.add(FileLabel(file_id=file.id, label_id=label.id, confidence=0.9))
    await db.commit()

    storage = StorageService()
    await storage.start()
    try:
        service = FileService(db, storage)
        await service.delete_file(user.id, file.id)  # used to raise AssertionError
    finally:
        await storage.close()

    assert await status_of(db, file.id) is None  # file row gone
    associations = (
        await db.execute(
            select(func.count())
            .select_from(FileLabel)
            .where(FileLabel.file_id == file.id)
        )
    ).scalar_one()
    assert associations == 0  # association rows cascaded away
    surviving = (
        await db.execute(select(Label).where(Label.id == label.id))
    ).scalar_one()
    assert surviving is not None  # the per-user label itself survives


# --- relay mapping + delete_object ---------------------------------------------------


async def test_relay_maps_storage_delete(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    key = f"{user.id}/{uuid.uuid4()}.txt"
    await seed_outbox_event(
        db, event_type="storage.delete", payload={"storage_key": key}
    )
    row_malformed = await seed_outbox_event(
        db, event_type="storage.delete", payload={}
    )
    await db.commit()

    broker = FakeBroker()
    engine, _ = make_factory()
    try:
        published = await relay_module._cycle(engine, broker)
    finally:
        await engine.dispose()

    assert published == 2  # both stamped; the malformed one dead-lettered by log
    assert ("delete_object", {"storage_key": key}) in broker.sent_kwargs
    assert ("delete_object", {"storage_key": ""}) not in broker.sent_kwargs
    stamped = (
        await db.execute(select(Outbox.published_at).where(Outbox.id == row_malformed.id))
    ).scalar_one()
    assert stamped is not None


async def test_delete_object_task_removes_and_is_idempotent(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    key = f"{user.id}/{uuid.uuid4()}.bin"
    storage = StorageService()
    await storage.start()
    try:
        await storage.put_bytes(key, b"to be removed")
        assert await storage.head(key) is not None
    finally:
        await storage.close()

    run_delete_task(key)

    storage = StorageService()
    await storage.start()
    try:
        assert await storage.head(key) is None
    finally:
        await storage.close()
    run_delete_task(key)  # duplicate delivery: missing object is a no-op


async def test_delete_object_poison_payload_is_swallowed(db: AsyncSession) -> None:
    run_delete_task("")  # logs ERROR, no raise, no storage call
