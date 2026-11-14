"""Phase 2 checkpoint: the DB itself rejects bad rows.

Every CHECK / unique / FK constraint the v3 schema declares is exercised by
inserting a violating row and asserting IntegrityError. Each violation runs
inside a SAVEPOINT (begin_nested): the bad row is undone, prior fixture rows
survive, and the outer fixture rollback leaves zero residue.

Requires: compose postgres + `alembic upgrade head`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import (
    File,
    FileKind,
    FileLabel,
    FileStatus,
    Folder,
    IdempotencyKey,
    Label,
    LabelKind,
    Permission,
    PermissionRole,
    RefreshToken,
    ShareLink,
    User,
)


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """Rollback-only session (same pattern as test_db_integration):
    NullPool + fresh engine per test → no cross-loop pooled sockets; the final
    rollback discards everything the test flushed."""
    engine = create_async_engine(_url(), poolclass=NullPool)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, autoflush=False, expire_on_commit=False
    )
    try:
        async with factory() as session:
            try:
                yield session
            finally:
                await session.rollback()
    finally:
        await engine.dispose()


def _url() -> str:
    import os

    return os.environ["DATABASE_URL"]


# --- row factories (flush immediately so savepoints only undo violations) ----


async def _user(db: AsyncSession, email: str) -> User:
    user = User(email=email, password_hash="x")
    db.add(user)
    await db.flush()
    return user


async def _folder(db: AsyncSession, owner: User, name: str) -> Folder:
    folder = Folder(owner_id=owner.id, name=name)
    db.add(folder)
    await db.flush()
    return folder


async def _file(db: AsyncSession, owner: User, name: str) -> File:
    file = File(
        user_id=owner.id,
        original_name=name,
        mime_type="application/octet-stream",
        size_bytes=1,
        storage_key=f"{owner.id}/{name}",
        status=FileStatus.UPLOADED,
    )
    db.add(file)
    await db.flush()
    return file


async def _violates(db: AsyncSession, row: object) -> None:
    """Add + flush inside a savepoint; the IntegrityError must escape and the
    savepoint must undo ONLY the bad row."""
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(row)
            await db.flush()


# --- permissions: exactly one of file_id / folder_id -----------------------------


async def test_permissions_both_targets_rejected(db: AsyncSession) -> None:
    owner = await _user(db, "xor-both@example.com")
    other = await _user(db, "xor-both-grantee@example.com")
    file = await _file(db, owner, "a.bin")
    folder = await _folder(db, owner, "a")
    await _violates(
        db,
        Permission(
            grantee_id=other.id,
            granted_by_id=owner.id,
            role=PermissionRole.VIEWER,
            file_id=file.id,
            folder_id=folder.id,  # BOTH set
        ),
    )


async def test_permissions_no_target_rejected(db: AsyncSession) -> None:
    owner = await _user(db, "xor-none@example.com")
    other = await _user(db, "xor-none-grantee@example.com")
    await _violates(
        db,
        Permission(
            grantee_id=other.id,
            granted_by_id=owner.id,
            role=PermissionRole.VIEWER,
            # BOTH null
        ),
    )


async def test_permissions_single_file_target_ok(db: AsyncSession) -> None:
    owner = await _user(db, "xor-file-ok@example.com")
    other = await _user(db, "xor-file-ok-grantee@example.com")
    file = await _file(db, owner, "ok.bin")
    db.add(
        Permission(
            grantee_id=other.id, granted_by_id=owner.id,
            role=PermissionRole.VIEWER, file_id=file.id,
        )
    )
    await db.flush()  # must NOT raise


async def test_permissions_duplicate_grantee_target_rejected(db: AsyncSession) -> None:
    owner = await _user(db, "dup-grant@example.com")
    other = await _user(db, "dup-grant-grantee@example.com")
    folder = await _folder(db, owner, "dup")
    db.add(
        Permission(
            grantee_id=other.id, granted_by_id=owner.id,
            role=PermissionRole.VIEWER, folder_id=folder.id,
        )
    )
    await db.flush()
    await _violates(
        db,
        Permission(
            grantee_id=other.id, granted_by_id=owner.id,
            role=PermissionRole.EDITOR, folder_id=folder.id,  # same (grantee, folder)
        ),
    )


# --- share_links: exactly one of file_id / folder_id -----------------------------


async def test_share_links_both_targets_rejected(db: AsyncSession) -> None:
    owner = await _user(db, "link-both@example.com")
    file = await _file(db, owner, "b.bin")
    folder = await _folder(db, owner, "b")
    await _violates(
        db,
        ShareLink(
            created_by_id=owner.id,
            token_hash="2" * 64,
            file_id=file.id,
            folder_id=folder.id,  # BOTH set
            allow_download=True,
        ),
    )


async def test_share_links_token_hash_unique(db: AsyncSession) -> None:
    owner = await _user(db, "link-tok@example.com")
    file = await _file(db, owner, "tok.bin")
    db.add(
        ShareLink(
            created_by_id=owner.id, token_hash="3" * 64,
            file_id=file.id, allow_download=True,
        )
    )
    await db.flush()
    await _violates(
        db,
        ShareLink(
            created_by_id=owner.id, token_hash="3" * 64,  # same hash
            file_id=file.id, allow_download=True,
        ),
    )


# --- users: citext case-insensitive uniqueness -------------------------------------


async def test_email_unique_case_insensitive(db: AsyncSession) -> None:
    await _user(db, "citext-check@example.com")
    await _violates(db, User(email="CITEXT-CHECK@example.com", password_hash="x"))


# --- refresh_tokens: token hash unique ----------------------------------------------


async def test_refresh_token_hash_unique(db: AsyncSession) -> None:
    owner = await _user(db, "rt-hash@example.com")
    expires = datetime.now(UTC) + timedelta(days=1)
    db.add(
        RefreshToken(user_id=owner.id, token_hash="4" * 64, expires_at=expires)
    )
    await db.flush()
    await _violates(
        db, RefreshToken(user_id=owner.id, token_hash="4" * 64, expires_at=expires)
    )


# --- labels: one label per (user, name, kind) ------------------------------------------


async def test_label_user_name_kind_unique(db: AsyncSession) -> None:
    owner = await _user(db, "label-uq@example.com")
    db.add(Label(user_id=owner.id, name="sunset", kind=LabelKind.THING))
    await db.flush()
    await _violates(db, Label(user_id=owner.id, name="sunset", kind=LabelKind.THING))
    # Different kind with same name is still allowed (v2 semantics carried).
    db.add(Label(user_id=owner.id, name="sunset", kind=LabelKind.DOC_TOPIC))
    await db.flush()


# --- file_labels: composite PK (file, label) ---------------------------------------------


async def test_file_label_duplicate_rejected(db: AsyncSession) -> None:
    owner = await _user(db, "fl-dup@example.com")
    file = await _file(db, owner, "c.bin")
    label = Label(user_id=owner.id, name="mountain", kind=LabelKind.THING)
    db.add(label)
    await db.flush()
    db.add(FileLabel(file_id=file.id, label_id=label.id, confidence=0.9))
    await db.flush()
    await _violates(
        db, FileLabel(file_id=file.id, label_id=label.id, confidence=0.5)
    )


# --- files: FK enforcement + generated search_vector ---------------------------------------


async def test_file_unknown_folder_fk_rejected(db: AsyncSession) -> None:
    owner = await _user(db, "fk-file@example.com")
    file = File(
        user_id=owner.id,
        folder_id=uuid.uuid4(),  # no such folder
        original_name="d.bin",
        mime_type="application/octet-stream",
        size_bytes=1,
        storage_key="d",
        status=FileStatus.UPLOADED,
    )
    await _violates(db, file)


async def test_search_vector_generated_from_name_and_text(db: AsyncSession) -> None:
    owner = await _user(db, "tsv@example.com")
    file = File(
        user_id=owner.id,
        original_name="Quarterly Report 2026",
        mime_type="application/pdf",
        size_bytes=1,
        storage_key="tsv",
        status=FileStatus.DONE,
        kind=FileKind.PDF,
        extracted_text="invoice totals and receipts",
    )
    db.add(file)
    await db.flush()

    row = await db.execute(select(File.search_vector).where(File.id == file.id))
    stored = row.scalar_one()
    expected = (
        await db.execute(
            sa.text(
                "SELECT to_tsvector('english', "
                "'Quarterly Report 2026 invoice totals and receipts')"
            )
        )
    ).scalar_one()
    assert stored is not None
    assert stored == expected


async def test_search_vector_survives_null_text(db: AsyncSession) -> None:
    """extracted_text NULL → tsvector built from the name alone (coalesce)."""
    owner = await _user(db, "tsv-null@example.com")
    file = await _file(db, owner, "Name Only Doc.pdf")
    file.mime_type = "application/pdf"  # keep fields coherent; kind stays NULL
    row = await db.execute(select(File.search_vector).where(File.id == file.id))
    stored = row.scalar_one()
    # coalesce(name,'') || ' ' || coalesce(text,'') → trailing space when
    # extracted_text is NULL — the raw expression, verbatim.
    expected = (
        await db.execute(sa.text("SELECT to_tsvector('english', 'Name Only Doc.pdf ')"))
    ).scalar_one()
    assert stored is not None
    assert stored == expected


# --- folders: ON DELETE CASCADE (explicit-ON-DELETE evidence) ----------------------------


async def test_folder_delete_cascades_subtree_and_files(db: AsyncSession) -> None:
    owner = await _user(db, "cascade@example.com")
    parent = await _folder(db, owner, "parent")
    child = Folder(owner_id=owner.id, parent_id=parent.id, name="child")
    db.add(child)
    await db.flush()
    root = await _file(db, owner, "inside.bin")  # root-level file must SURVIVE
    inner = File(
        user_id=owner.id,
        folder_id=child.id,
        original_name="inner.bin",
        mime_type="application/octet-stream",
        size_bytes=1,
        storage_key="inner",
        status=FileStatus.UPLOADED,
    )
    db.add(inner)
    await db.flush()

    await db.delete(parent)
    await db.flush()

    # Proof must bypass the identity map: db.get() would return the stale
    # in-memory objects even though the DB already cascade-deleted their rows.
    # Plain SELECTs hit the DB and show what ON DELETE CASCADE actually did.
    child_after = (
        await db.execute(select(Folder).where(Folder.id == child.id))
    ).scalar_one_or_none()
    inner_after = (
        await db.execute(select(File).where(File.id == inner.id))
    ).scalar_one_or_none()
    root_after = (
        await db.execute(select(File).where(File.id == root.id))
    ).scalar_one_or_none()
    assert child_after is None  # child folder: cascade-deleted
    assert inner_after is None  # file inside child: cascade-deleted
    assert root_after is not None  # root-level file of the same user: untouched


# --- idempotency_keys: one key per user ------------------------------------------------


async def test_idempotency_key_user_key_unique(db: AsyncSession) -> None:
    owner = await _user(db, "idem@example.com")
    expires = datetime.now(UTC) + timedelta(hours=24)
    db.add(
        IdempotencyKey(
            user_id=owner.id,
            key="abc",
            request_hash="5" * 64,
            response_status=201,
            response_body={},
            expires_at=expires,
        )
    )
    await db.flush()
    await _violates(
        db,
        IdempotencyKey(
            user_id=owner.id,  # same user + key
            key="abc",
            request_hash="6" * 64,
            response_status=201,
            response_body={},
            expires_at=expires,
        ),
    )
    # A different user may reuse the same key value (scoped per user).
    other = await _user(db, "idem-other@example.com")
    db.add(
        IdempotencyKey(
            user_id=other.id,
            key="abc",
            request_hash="7" * 64,
            response_status=201,
            response_body={},
            expires_at=expires,
        )
    )
    await db.flush()
