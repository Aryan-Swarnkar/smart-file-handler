"""File-target authorization — resolve_file_role through AccessService.

The Phase 4 commit shipped the file-side resolver (ownership + direct file
grant OR grant on any ancestor folder + trash chain) with ZERO tests. Closed
here: Phase 5/8 build on it, and untested authz SQL is unshippable. Tests
call AccessService.resolve_role with File rows exactly the way services will.

File rows are seeded directly (resolve never touches S3 — no storage
round-trip needed). File deleted_at is exercised by setting the column: the
trash endpoint itself is a later phase, but the RULE ships now.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import File, PermissionRole
from app.services.access_service import AccessRole, AccessService
from tests.authz_matrix.conftest import (
    Tree,
    commit,
    seed_grant,
    seed_user,
)


async def seed_file(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    folder_id: uuid.UUID | None = None,
    name: str | None = None,
) -> File:
    file = File(
        user_id=owner_id,
        folder_id=folder_id,
        original_name=name or f"file-{uuid.uuid4().hex[:8]}.bin",
        mime_type="application/octet-stream",
        size_bytes=1,
        storage_key=f"{owner_id}/{uuid.uuid4()}",
    )
    db.add(file)
    await db.flush()
    return file


async def test_owner_resolves_owner_top_level(
    db: AsyncSession, tree: Tree
) -> None:
    """Own file at the drive top level (folder_id NULL, empty chain)."""
    file = await seed_file(db, tree.owner.id)
    await commit(db)
    access = AccessService(db)
    assert await access.resolve_role(tree.owner.id, file) is AccessRole.OWNER


async def test_stranger_gets_none(db: AsyncSession, tree: Tree) -> None:
    file = await seed_file(db, tree.owner.id)
    await commit(db)
    access = AccessService(db)
    assert await access.resolve_role(tree.stranger.id, file) is None


async def test_direct_grants_resolve(
    db: AsyncSession, tree: Tree
) -> None:
    for role, expected in (
        (PermissionRole.VIEWER, AccessRole.VIEWER),
        (PermissionRole.EDITOR, AccessRole.EDITOR),
    ):
        file = await seed_file(db, tree.owner.id)
        grantee = await seed_user(db)
        await seed_grant(db, grantee.id, role, file)
        await commit(db)
        access = AccessService(db)
        assert (
            await access.resolve_role(grantee.id, file) is expected
        ), f"{role} grant must resolve {expected}"


async def test_grant_on_containing_folder_inherits(
    db: AsyncSession, tree: Tree
) -> None:
    """Grant on the FOLDER (not the file) reaches files inside via the
    ancestor chain."""
    file = await seed_file(db, tree.owner.id, folder_id=tree.target.id)
    grantee = await seed_user(db)
    await seed_grant(db, grantee.id, PermissionRole.VIEWER, tree.target)
    await commit(db)
    access = AccessService(db)
    assert await access.resolve_role(grantee.id, file) is AccessRole.VIEWER


async def test_grant_on_root_reaches_deep_file(
    db: AsyncSession, tree: Tree
) -> None:
    """Grant on the ROOT folder reaches a file two levels down
    (root → parent → target)."""
    file = await seed_file(db, tree.owner.id, folder_id=tree.target.id)
    grantee = await seed_user(db)
    await seed_grant(db, grantee.id, PermissionRole.EDITOR, tree.root)
    await commit(db)
    access = AccessService(db)
    assert await access.resolve_role(grantee.id, file) is AccessRole.EDITOR


async def test_best_grant_wins_across_file_and_folders(
    db: AsyncSession, tree: Tree
) -> None:
    """Viewer direct on the file + editor on the folder → the single
    statement ranks across BOTH targets; editor must win."""
    file = await seed_file(db, tree.owner.id, folder_id=tree.target.id)
    grantee = await seed_user(db)
    await seed_grant(db, grantee.id, PermissionRole.VIEWER, file)
    await seed_grant(db, grantee.id, PermissionRole.EDITOR, tree.target)
    await commit(db)
    access = AccessService(db)
    assert await access.resolve_role(grantee.id, file) is AccessRole.EDITOR


async def test_trashed_file_owner_kept_grantee_hidden(
    db: AsyncSession, tree: Tree
) -> None:
    """deleted_at on the file: the owner keeps access (their trash to
    manage); a grantee with a valid direct grant loses it entirely."""
    file = await seed_file(db, tree.owner.id)
    grantee = await seed_user(db)
    await seed_grant(db, grantee.id, PermissionRole.EDITOR, file)
    await commit(db)

    file.deleted_at = datetime.now(UTC)
    await commit(db)

    access = AccessService(db)
    assert await access.resolve_role(tree.owner.id, file) is AccessRole.OWNER
    assert await access.resolve_role(grantee.id, file) is None


async def test_file_under_trashed_ancestor(
    db: AsyncSession, tree: Tree
) -> None:
    """Trashed ANCESTOR folder hides its files from grantees (grant on the
    folder itself — normally inherited) while the owner keeps access."""
    file = await seed_file(db, tree.owner.id, folder_id=tree.target.id)
    grantee = await seed_user(db)
    await seed_grant(db, grantee.id, PermissionRole.EDITOR, tree.target)
    await commit(db)

    tree.target.deleted_at = datetime.now(UTC)
    await commit(db)

    access = AccessService(db)
    assert await access.resolve_role(grantee.id, file) is None
    assert await access.resolve_role(tree.owner.id, file) is AccessRole.OWNER


async def test_nonexistent_file_resolves_none(
    db: AsyncSession, tree: Tree
) -> None:
    """An unsaved File object (id points nowhere) → None, never a crash —
    the 404 mapping happens in the service layer."""
    ghost = File(
        id=uuid.uuid4(),
        user_id=tree.owner.id,
        original_name="ghost.bin",
        mime_type="application/octet-stream",
        size_bytes=1,
        storage_key="ghost",
    )
    access = AccessService(db)
    assert await access.resolve_role(tree.owner.id, ghost) is None
