"""Shared seeding for the authz matrix tests.

Fast path: users/folders/grants are inserted DIRECTLY (no HTTP registration,
no argon2) and access tokens are minted in-process via
services.security.create_access_token — the matrix needs ~50 API actors per
run, which would drown in registration round-trips otherwise. Each test
builds a FRESH tree (function scope) because rename/move/trash/restore
mutate it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db.models import Folder, Permission, PermissionRole, User
from app.repositories.folders import FolderRepository
from app.repositories.permissions import PermissionRepository
from app.services.security import create_access_token


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(get_settings().DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


async def seed_user(db: AsyncSession) -> User:
    """One account row directly in the DB. password_hash is a dummy — these
    users never log in through the API (tokens are minted in-process)."""
    user = User(
        email=f"azx-{uuid.uuid4().hex[:12]}@example.com", password_hash="x"
    )
    db.add(user)
    await db.flush()
    return user


async def seed_folder(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    parent_id: uuid.UUID | None = None,
    name: str | None = None,
) -> Folder:
    folder = await FolderRepository(db).create(
        owner_id, name or f"folder-{uuid.uuid4().hex[:8]}", parent_id=parent_id
    )
    return folder


async def seed_grant(
    db: AsyncSession,
    grantee_id: uuid.UUID,
    role: PermissionRole,
    folder: Folder,
) -> Permission:
    return await PermissionRepository(db).create_grant(
        grantee_id=grantee_id, role=role, folder_id=folder.id
    )


def headers_for(user: User) -> dict[str, str]:
    token, _ = create_access_token(user.id)
    return {"Authorization": f"Bearer {token}"}


async def commit(db: AsyncSession) -> None:
    """Seed helper: the API runs on ITS OWN session — test rows must be
    committed before any request can see them."""
    await db.commit()


class Tree(SimpleNamespace):
    """root ── parent ── target ── child        (target is the matrix subject)
             └─ sibling                      (move destination)"""

    owner: User
    stranger: User
    root: Folder
    parent: Folder
    target: Folder
    child: Folder
    sibling: Folder


@pytest.fixture
async def tree(db: AsyncSession) -> AsyncGenerator[Tree, None]:
    owner = await seed_user(db)
    stranger = await seed_user(db)
    root = await seed_folder(db, owner.id, name="My Files")
    parent = await seed_folder(db, owner.id, parent_id=root.id, name="parent")
    target = await seed_folder(db, owner.id, parent_id=parent.id, name="target")
    child = await seed_folder(db, owner.id, parent_id=target.id, name="child")
    sibling = await seed_folder(db, owner.id, parent_id=root.id, name="sibling")
    await commit(db)
    yield Tree(
        owner=owner,
        stranger=stranger,
        root=root,
        parent=parent,
        target=target,
        child=child,
        sibling=sibling,
    )
