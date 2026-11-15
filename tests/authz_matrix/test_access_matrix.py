"""The authz matrix: roles x operations through the REAL API.

Locked decisions (Plan review 2026-11-15):
- owner = everything; editor (direct or ancestor-inherited) = list/read/
  rename/move; viewer = list/read; stranger = all 404; trash/restore are
  OWNER-ONLY.
- Every denial is 404 (never 403) — asserted per cell.
- A trashed node (self or ancestor) blocks mutations for the OWNER (422
  trashed) and hides the subtree from everyone else (404); trash/restore
  are idempotent (repeat → 204).

Each cell builds a FRESH tree (the mutating ops would otherwise leak state
into the next cell) and plants exactly the grants the role label promises:
direct = grant on the target itself, inherited = grant on its parent.
Granted roles additionally get an EDITOR grant on the sibling (the move
destination) so a cell's failure is always attributable to its own role —
destination access is isolated in test_move_rules.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

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

OPS = ("list", "read", "rename", "move", "trash", "restore")

NO_WRITE = {"trash": 404, "restore": 404}

# (role, op) -> expected status
EXPECTED: dict[str, dict[str, int]] = {
    "owner": {"list": 200, "read": 200, "rename": 200, "move": 200, **NO_WRITE}
    | {"trash": 204, "restore": 204},
    "editor_direct": {"list": 200, "read": 200, "rename": 200, "move": 200, **NO_WRITE},
    "editor_inherited": {
        "list": 200,
        "read": 200,
        "rename": 200,
        "move": 200,
        **NO_WRITE,
    },
    "viewer_direct": {"list": 200, "read": 200, "rename": 404, "move": 404, **NO_WRITE},
    "viewer_inherited": {
        "list": 200,
        "read": 200,
        "rename": 404,
        "move": 404,
        **NO_WRITE,
    },
    "stranger": dict.fromkeys(OPS, 404),
}

# Rows on a trashed tree. owner_self_trashed: the TARGET carries deleted_at.
# owner_trashed_ancestor: the PARENT is trashed, the target is live.
EXPECTED_TRASHED: dict[str, dict[str, int]] = {
    "owner_self_trashed": {
        "list": 200,
        "read": 200,
        "rename": 422,
        "move": 422,
        "trash": 204,  # idempotent re-trash
        "restore": 204,
    },
    "owner_trashed_ancestor": {
        "list": 200,
        "read": 200,
        "rename": 422,
        "move": 422,
        "trash": 422,  # restore the ancestor first
        "restore": 204,  # idempotent (target itself is live)
    },
    "editor_trashed_ancestor": dict.fromkeys(OPS, 404),
    "viewer_trashed": dict.fromkeys(OPS, 404),
}


async def _prepare(
    db: AsyncSession, tree: Tree, role: str
) -> dict[str, str]:
    """Resolve the actor for a role label, plant its grants, commit, return
    bearer headers."""
    trashed_variant = role in EXPECTED_TRASHED
    base_role = role
    if trashed_variant:
        # strip the trash suffix to reuse the same grant logic
        base_role = (
            "owner"
            if role.startswith("owner")
            else ("editor_inherited" if role.startswith("editor") else "viewer_direct")
        )

    if base_role == "owner":
        actor = tree.owner
    elif base_role == "stranger":
        actor = tree.stranger
    else:
        actor = await seed_user(db)

    if base_role.startswith("editor") or base_role.startswith("viewer"):
        level = (
            PermissionRole.EDITOR
            if base_role.startswith("editor")
            else PermissionRole.VIEWER
        )
        if base_role.endswith("_direct"):
            await seed_grant(db, actor.id, level, tree.target)
        else:  # inherited: grant sits on the ancestor
            await seed_grant(db, actor.id, level, tree.parent)
        # destination access for the move cell (isolated rule → test_move_rules)
        await seed_grant(db, actor.id, PermissionRole.EDITOR, tree.sibling)

    if trashed_variant:
        trashed_at = datetime.now(UTC)
        if role == "owner_self_trashed" or role == "viewer_trashed":
            tree.target.deleted_at = trashed_at
        else:  # owner_trashed_ancestor / editor_trashed_ancestor
            tree.parent.deleted_at = trashed_at

    await commit(db)
    return headers_for(actor)


async def _run_op(
    client: AsyncClient, headers: dict[str, str], tree: Tree, op: str
) -> tuple[int, object]:
    if op == "list":
        resp = await client.get(
            f"/folders/{tree.target.id}/children", headers=headers
        )
        body: object = resp.json()
    elif op == "read":
        resp = await client.get(f"/folders/{tree.target.id}", headers=headers)
        body = resp.json()
    elif op == "rename":
        resp = await client.patch(
            f"/folders/{tree.target.id}",
            headers=headers,
            json={"name": "renamed"},
        )
        body = resp.json()
    elif op == "move":
        resp = await client.post(
            f"/folders/{tree.target.id}/move",
            headers=headers,
            json={"parent_id": str(tree.sibling.id)},
        )
        body = resp.json()
    elif op == "trash":
        resp = await client.post(f"/folders/{tree.target.id}/trash", headers=headers)
        body = None
    else:  # restore
        resp = await client.post(
            f"/folders/{tree.target.id}/restore", headers=headers
        )
        body = None
    return resp.status_code, body


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("role", list(EXPECTED))
async def test_matrix_cell(
    client: AsyncClient, db: AsyncSession, tree: Tree, role: str, op: str
) -> None:
    expected = EXPECTED[role][op]
    headers = await _prepare(db, tree, role)
    status, body = await _run_op(client, headers, tree, op)

    assert status != 403, "spec: denials are 404, never 403"
    assert status == expected, f"{role}.{op}: got {status}, {body!r}"

    # Content-level sanity on the meaningful successes (not just the code).
    if op == "list" and status == 200 and role != "stranger":
        ids = {item["id"] for item in body["items"]}  # type: ignore[index]
        assert str(tree.child.id) in ids, "accessible folder must list its child"
    if op == "rename" and status == 200:
        assert body["name"] == "renamed"  # type: ignore[index]
    if op == "move" and status == 200:
        assert body["parent_id"] == str(tree.sibling.id)  # type: ignore[index]


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("role", list(EXPECTED_TRASHED))
async def test_matrix_cell_trashed(
    client: AsyncClient, db: AsyncSession, tree: Tree, role: str, op: str
) -> None:
    expected = EXPECTED_TRASHED[role][op]
    headers = await _prepare(db, tree, role)
    status, body = await _run_op(client, headers, tree, op)

    assert status != 403
    assert status == expected, f"{role}.{op}: got {status}, {body!r}"
