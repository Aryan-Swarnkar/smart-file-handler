"""Move/destination rules, trash/restore edges — the cells the matrix
deliberately does NOT cover:

- destination access is its own rule (editor required; viewer → 404;
  nonexistent → 404; trashed dest → 422)
- cross-owner moves are forbidden (422) even with editor on both sides
- cycle family: into self, into a descendant, root under its own child
- no-op move (same parent) is a 200 that changes nothing
- root cannot be trashed; trash/restore are idempotent; restore under a
  still-trashed ancestor is 422 parent_trashed
- unauthenticated calls are 401, not 403/404
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PermissionRole
from tests.authz_matrix.conftest import (
    Tree,
    commit,
    headers_for,
    seed_grant,
    seed_user,
)


async def _grant(
    db: AsyncSession, user_id: uuid.UUID, role: PermissionRole, tree: Tree, folder: str
) -> None:
    await seed_grant(db, user_id, role, getattr(tree, folder))


# --- destination access ---------------------------------------------------------


async def test_move_requires_editor_on_destination(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    """Actor is editor on the folder but only VIEWER on the destination —
    placing an entry is a write to the destination, so 404."""
    actor = await seed_user(db)
    await _grant(db, actor.id, PermissionRole.EDITOR, tree, "target")
    await _grant(db, actor.id, PermissionRole.VIEWER, tree, "sibling")
    await commit(db)

    resp = await client.post(
        f"/folders/{tree.target.id}/move",
        headers=headers_for(actor),
        json={"parent_id": str(tree.sibling.id)},
    )
    assert resp.status_code == 404, resp.text


async def test_move_to_invisible_destination_404(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    actor = await seed_user(db)
    await _grant(db, actor.id, PermissionRole.EDITOR, tree, "target")
    await commit(db)
    resp = await client.post(
        f"/folders/{tree.target.id}/move",
        headers=headers_for(actor),
        json={"parent_id": str(tree.sibling.id)},  # sibling: no grant at all
    )
    assert resp.status_code == 404, resp.text


async def test_move_to_nonexistent_destination_404(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    resp = await client.post(
        f"/folders/{tree.target.id}/move",
        headers=headers_for(tree.owner),
        json={"parent_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404, resp.text


async def test_move_to_trashed_destination_422(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    tree.sibling.deleted_at = datetime.now(UTC)
    await commit(db)
    resp = await client.post(
        f"/folders/{tree.target.id}/move",
        headers=headers_for(tree.owner),
        json={"parent_id": str(tree.sibling.id)},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "destination_trashed"


# --- cycle family ---------------------------------------------------------------


async def test_move_into_self_422(client: AsyncClient, tree: Tree) -> None:
    resp = await client.post(
        f"/folders/{tree.target.id}/move",
        headers=headers_for(tree.owner),
        json={"parent_id": str(tree.target.id)},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "folder_cycle"


async def test_move_into_own_descendant_422(
    client: AsyncClient, tree: Tree
) -> None:
    resp = await client.post(
        f"/folders/{tree.target.id}/move",
        headers=headers_for(tree.owner),
        json={"parent_id": str(tree.child.id)},  # child of the target
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "folder_cycle"


async def test_move_root_under_its_own_child_422(
    client: AsyncClient, tree: Tree
) -> None:
    resp = await client.post(
        f"/folders/{tree.root.id}/move",
        headers=headers_for(tree.owner),
        json={"parent_id": str(tree.child.id)},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "folder_cycle"


# --- cross-owner ----------------------------------------------------------------


async def test_move_across_owners_422(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    """Actor has editor on the foreign folder (grant from its owner) and
    owns the destination — still forbidden: trees never merge."""
    from tests.authz_matrix.conftest import seed_folder

    actor = await seed_user(db)
    foreign_target = await seed_folder(
        db, tree.owner.id, parent_id=tree.root.id, name="foreign"
    )
    own_dest = await seed_folder(db, actor.id, parent_id=None, name="actor-root")
    await seed_grant(db, actor.id, PermissionRole.EDITOR, foreign_target)
    await commit(db)

    resp = await client.post(
        f"/folders/{foreign_target.id}/move",
        headers=headers_for(actor),
        json={"parent_id": str(own_dest.id)},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "cross_owner_move"


# --- non-mutating move rules ------------------------------------------------------


async def test_move_nonexistent_folder_404(client: AsyncClient, tree: Tree) -> None:
    resp = await client.post(
        f"/folders/{uuid.uuid4()}/move",
        headers=headers_for(tree.owner),
        json={"parent_id": str(tree.sibling.id)},
    )
    assert resp.status_code == 404, resp.text


async def test_noop_move_is_200_and_changes_nothing(
    client: AsyncClient, tree: Tree
) -> None:
    resp = await client.post(
        f"/folders/{tree.target.id}/move",
        headers=headers_for(tree.owner),
        json={"parent_id": str(tree.parent.id)},  # already its parent
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["parent_id"] == str(tree.parent.id)


# --- trash / restore edges ---------------------------------------------------------


async def test_root_cannot_be_trashed(client: AsyncClient, tree: Tree) -> None:
    resp = await client.post(
        f"/folders/{tree.root.id}/trash", headers=headers_for(tree.owner)
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "root_trash"


async def test_trash_then_restore_round_trip(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    r1 = await client.post(
        f"/folders/{tree.target.id}/trash", headers=headers_for(tree.owner)
    )
    r2 = await client.post(
        f"/folders/{tree.target.id}/trash", headers=headers_for(tree.owner)
    )
    assert (r1.status_code, r2.status_code) == (204, 204), "idempotent re-trash"

    # The subtree is hidden from the owner's normal browse... no: read stays
    # allowed for the owner; what dies is EDITOR/viewer access (matrix rows).
    read = await client.get(f"/folders/{tree.target.id}", headers=headers_for(tree.owner))
    assert read.status_code == 200
    assert read.json()["deleted_at"] is not None

    r3 = await client.post(
        f"/folders/{tree.target.id}/restore", headers=headers_for(tree.owner)
    )
    r4 = await client.post(
        f"/folders/{tree.target.id}/restore", headers=headers_for(tree.owner)
    )
    assert (r3.status_code, r4.status_code) == (204, 204), "idempotent re-restore"
    read = await client.get(f"/folders/{tree.target.id}", headers=headers_for(tree.owner))
    assert read.status_code == 200
    assert read.json()["deleted_at"] is None


async def test_restore_under_still_trashed_ancestor_422(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    """Trash parent AND target, restore target first → 422 parent_trashed;
    after restoring the ancestor the target restores fine."""
    tree.parent.deleted_at = datetime.now(UTC)
    tree.target.deleted_at = datetime.now(UTC)
    await commit(db)

    resp = await client.post(
        f"/folders/{tree.target.id}/restore", headers=headers_for(tree.owner)
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "parent_trashed"

    up = await client.post(
        f"/folders/{tree.parent.id}/restore", headers=headers_for(tree.owner)
    )
    assert up.status_code == 204, up.text
    resp = await client.post(
        f"/folders/{tree.target.id}/restore", headers=headers_for(tree.owner)
    )
    assert resp.status_code == 204, resp.text


# --- authentication ---------------------------------------------------------------


async def test_folders_require_bearer(client: AsyncClient, tree: Tree) -> None:
    for method, path, extra in (
        ("get", f"/folders/{tree.target.id}", None),
        ("post", f"/folders/{tree.target.id}/move", {"parent_id": str(tree.sibling.id)}),
        ("post", f"/folders/{tree.target.id}/trash", None),
    ):
        kwargs = {"json": extra} if extra is not None else {}
        resp = await getattr(client, method)(path, **kwargs)
        assert resp.status_code == 401, f"{method} {path}: {resp.status_code}"
