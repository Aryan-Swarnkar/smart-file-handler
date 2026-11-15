"""Depth cap 20 (docs/adr/0001): creates and moves that would push any
root→leaf path past 20 nodes are rejected; a grant at the TOP of a maximal
(depth-20) chain still resolves through the full ancestor CTE.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PermissionRole
from app.services.access_service import AccessRole, AccessService
from tests.authz_matrix.conftest import (
    Tree,
    commit,
    headers_for,
    seed_folder,
    seed_grant,
    seed_user,
)


async def _chain(
    db: AsyncSession,
    owner_id: uuid.UUID,
    parent_id: uuid.UUID,
    length: int,
) -> list:
    """`length` folders hanging under parent_id, deepest last."""
    nodes: list = []
    current = parent_id
    for i in range(length):
        node = await seed_folder(db, owner_id, parent_id=current, name=f"d{i:02d}")
        nodes.append(node)
        current = node.id
    return nodes


# --- create ---------------------------------------------------------------------


async def test_create_at_the_cap_ok_beyond_422(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    """tree.root is level 1; a chain of 18 under it ends at level 19. One
    more create succeeds (level 20 = the cap); anything under THAT would be
    21 → 422."""
    owner = tree.owner
    chain = await _chain(db, owner.id, tree.root.id, 18)
    await commit(db)
    level19 = chain[-1]

    ok = await client.post(
        "/folders",
        headers=headers_for(owner),
        json={"name": "level-20", "parent_id": str(level19.id)},
    )
    assert ok.status_code == 201, ok.text

    too_deep = await client.post(
        "/folders",
        headers=headers_for(owner),
        json={"name": "level-21", "parent_id": str(ok.json()["id"])},
    )
    assert too_deep.status_code == 422, too_deep.text
    assert too_deep.json()["error"]["code"] == "depth_limit"


async def test_create_editor_charged_owner_tree(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    """Creating inside a folder shared with you as editor grows the PARENT
    owner's tree (folder.owner_id = parent's owner), not yours."""

    editor = await seed_user(db)
    await seed_grant(db, editor.id, PermissionRole.EDITOR, tree.parent)
    await commit(db)

    resp = await client.post(
        "/folders",
        headers=headers_for(editor),
        json={"name": "editors-folder", "parent_id": str(tree.parent.id)},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["owner_id"] == str(tree.owner.id)


# --- move -----------------------------------------------------------------------


async def test_move_depth_guard(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    """A height-3 subtree moved under a level-17 destination (17+3=20) is
    the last legal slot; level 18 (18+3=21) is rejected."""
    owner = tree.owner
    # destination chain d00..d16 under the root: d16 is level 18, d15 is 17
    dest_chain = await _chain(db, owner.id, tree.root.id, 17)
    await commit(db)
    level17, level18 = dest_chain[15], dest_chain[16]

    # moving subtree: s1 → s2 → s3 under the root (height 3)
    s1 = await seed_folder(db, owner.id, parent_id=tree.root.id, name="s1")
    s2 = await seed_folder(db, owner.id, parent_id=s1.id, name="s2")
    await seed_folder(db, owner.id, parent_id=s2.id, name="s3")
    await commit(db)

    illegal = await client.post(
        f"/folders/{s1.id}/move",
        headers=headers_for(owner),
        json={"parent_id": str(level18.id)},  # 18 + 3 = 21 > 20
    )
    assert illegal.status_code == 422, illegal.text
    assert illegal.json()["error"]["code"] == "depth_limit"

    legal = await client.post(
        f"/folders/{s1.id}/move",
        headers=headers_for(owner),
        json={"parent_id": str(level17.id)},  # 17 + 3 = 20 = cap
    )
    assert legal.status_code == 200, legal.text


# --- role resolution at max depth -------------------------------------------------


async def test_grant_on_root_resolves_at_depth_20(
    db: AsyncSession, tree: Tree
) -> None:
    """The CTE covers self + 19 ancestors exactly when the tree is maximal:
    an editor grant on the ROOT must surface on the depth-20 leaf."""
    owner = tree.owner
    chain = await _chain(db, owner.id, tree.root.id, 19)  # root + 19 = level 20
    await commit(db)
    leaf = chain[-1]

    grantee = await seed_user(db)
    await seed_grant(db, grantee.id, PermissionRole.EDITOR, tree.root)
    await commit(db)

    access = AccessService(db)
    role = await access.resolve_folder_role(grantee.id, leaf.id)
    assert role is AccessRole.EDITOR

    # And ownership resolves at any depth without touching the CTE's tail.
    role_owner = await access.resolve_folder_role(owner.id, leaf.id)
    assert role_owner is AccessRole.OWNER


async def test_grant_beyond_cap_would_not_resolve_but_is_unreachable(
    db: AsyncSession, tree: Tree
) -> None:
    """A malformed >20 chain (only craftable by bypassing the service) gets
    its tail truncated by the CTE — a grant near the root stops resolving
    from nodes deeper than the window. Documents the cap's effect instead
    of pretending it cannot happen."""
    owner = tree.owner
    chain = await _chain(db, owner.id, tree.root.id, 19)
    await commit(db)
    leaf = chain[-1]  # level 20 — the deepest legal node
    # Craft an illegal 25-deep chain directly (the service can never do
    # this — creates and moves are depth-guarded).
    deep = leaf
    for i in range(5):
        nxt = await seed_folder(db, owner.id, parent_id=deep.id, name=f"x{i}")
        deep = nxt
    await commit(db)

    grantee = await seed_user(db)
    await seed_grant(db, grantee.id, PermissionRole.EDITOR, tree.root)
    await commit(db)

    access = AccessService(db)
    # From the level-20 leaf the chain is exactly 20 rows (leaf..root), so
    # the root grant is at the window's edge → resolves.
    assert await access.resolve_folder_role(grantee.id, leaf.id) is AccessRole.EDITOR

    # From the level-25 node the window is truncated at 20 rows — it reaches
    # only up to level 6, so the root grant (level 1) falls outside → None.
    role = await access.resolve_folder_role(grantee.id, deep.id)
    assert role is None
