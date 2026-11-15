"""Create-op authz cells + validation edges — the ops the matrix table does
NOT parametrize (OPS covers list/read/rename/move/trash/restore; create has
different request semantics, so it gets dedicated cells):

- create requires EDITOR on the parent: viewer → 404, stranger → 404
- create inside a trashed chain: owner → 422 trashed (state conflict),
  editor-with-grant → 404 (trashed subtree is invisible to non-owners)
- whitespace-only names are 422 empty_name on create AND rename
  (schema min_length=1 admits " "; the service strips then rejects)
- duplicate sibling names are legal (no unique constraint) and both must
  list exactly once — pins the (name, id) keyset tiebreak in listings
"""

from __future__ import annotations

import pytest
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


async def test_create_by_viewer_404(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    actor = await seed_user(db)
    await seed_grant(db, actor.id, PermissionRole.VIEWER, tree.parent)
    await commit(db)
    resp = await client.post(
        "/folders",
        headers=headers_for(actor),
        json={"name": "nope", "parent_id": str(tree.parent.id)},
    )
    assert resp.status_code == 404, resp.text
    assert resp.status_code != 403


async def test_create_by_stranger_404(
    client: AsyncClient, tree: Tree
) -> None:
    resp = await client.post(
        "/folders",
        headers=headers_for(tree.stranger),
        json={"name": "nope", "parent_id": str(tree.parent.id)},
    )
    assert resp.status_code == 404, resp.text
    assert resp.status_code != 403


async def test_create_inside_trashed_parent_owner_422(
    client: AsyncClient, tree: Tree
) -> None:
    trash = await client.post(
        f"/folders/{tree.parent.id}/trash", headers=headers_for(tree.owner)
    )
    assert trash.status_code == 204, trash.text
    resp = await client.post(
        "/folders",
        headers=headers_for(tree.owner),
        json={"name": "nope", "parent_id": str(tree.parent.id)},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "trashed"


async def test_create_inside_trashed_parent_editor_404(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    """The grant survives, but a trashed chain is invisible to non-owners:
    create must 404, never 422 (422 would confirm the folder exists)."""
    actor = await seed_user(db)
    await seed_grant(db, actor.id, PermissionRole.EDITOR, tree.parent)
    await commit(db)
    trash = await client.post(
        f"/folders/{tree.parent.id}/trash", headers=headers_for(tree.owner)
    )
    assert trash.status_code == 204, trash.text
    resp = await client.post(
        "/folders",
        headers=headers_for(actor),
        json={"name": "nope", "parent_id": str(tree.parent.id)},
    )
    assert resp.status_code == 404, resp.text
    assert resp.status_code != 403


@pytest.mark.parametrize("op", ["create", "rename"])
async def test_whitespace_only_name_422(
    client: AsyncClient, tree: Tree, op: str
) -> None:
    if op == "create":
        resp = await client.post(
            "/folders",
            headers=headers_for(tree.owner),
            json={"name": "   ", "parent_id": str(tree.parent.id)},
        )
    else:
        resp = await client.patch(
            f"/folders/{tree.target.id}",
            headers=headers_for(tree.owner),
            json={"name": "   "},
        )
    assert resp.status_code == 422, f"{op}: {resp.text}"
    assert resp.json()["error"]["code"] == "empty_name", resp.text


async def test_duplicate_sibling_names_both_listed(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    """No unique constraint on (parent_id, name) — Drive allows duplicates.
    Both rows must appear exactly once in the listing (id tiebreak)."""
    r1 = await client.post(
        "/folders",
        headers=headers_for(tree.owner),
        json={"name": "dup", "parent_id": str(tree.parent.id)},
    )
    r2 = await client.post(
        "/folders",
        headers=headers_for(tree.owner),
        json={"name": "dup", "parent_id": str(tree.parent.id)},
    )
    assert r1.status_code == 201 and r2.status_code == 201, (r1.text, r2.text)
    assert r1.json()["id"] != r2.json()["id"]

    listing = await client.get(
        f"/folders/{tree.parent.id}/children",
        headers=headers_for(tree.owner),
        params={"limit": 50},
    )
    names = [i["name"] for i in listing.json()["items"]]
    assert names.count("dup") == 2, names
