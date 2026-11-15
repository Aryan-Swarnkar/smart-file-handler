"""Keyset pagination correctness (name ASC / created_at DESC, never OFFSET):

- full walks see every seeded row exactly once, in sort order
- limit=1 walking (cursor advanced per row)
- rows inserted MID-WALK never corrupt the walk: originals appear exactly
  once; rows sorting before the cursor are (correctly) not picked up
- a cursor minted for one sort is rejected under the other sort (422)
- trashed children are excluded from listings
- tampered cursors are 422, not 500

Walks use a DEDICATED parent folder: the tree fixture hangs its own
target/child off tree.parent, which would pollute exact-count assertions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Folder
from tests.authz_matrix.conftest import (
    Tree,
    commit,
    headers_for,
    seed_folder,
)


def _owner_headers(tree: Tree) -> dict[str, str]:
    return headers_for(tree.owner)


async def _fresh_parent(db: AsyncSession, tree: Tree) -> Folder:
    """A dedicated parent under the root, empty and owned by the fixture user."""
    parent = await seed_folder(
        db, tree.owner.id, parent_id=tree.root.id, name="browse"
    )
    await commit(db)
    return parent


async def _walk_all(
    client: AsyncClient,
    tree: Tree,
    parent: Folder,
    *,
    sort: str,
    limit: int,
) -> list[dict]:
    items: list[dict] = []
    cursor: str | None = None
    for _ in range(200):  # hard bound — a broken cursor must not loop forever
        params: dict[str, str | int] = {"limit": limit, "sort": sort}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await client.get(
            f"/folders/{parent.id}/children",
            headers=_owner_headers(tree),
            params=params,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return items
    pytest.fail("pagination walk exceeded 200 pages — cursor never terminated")


async def _seed_children(
    db: AsyncSession, tree: Tree, parent: Folder, names: list[str]
) -> None:
    for name in names:
        await seed_folder(db, tree.owner.id, parent_id=parent.id, name=name)
    await commit(db)


# --- name sort -----------------------------------------------------------------


async def test_name_sort_full_walk_exact_order(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    parent = await _fresh_parent(db, tree)
    names = [f"f-{i:02d}" for i in range(30)]
    await _seed_children(db, tree, parent, names)

    items = await _walk_all(client, tree, parent, sort="name", limit=7)
    got = [i["name"] for i in items]
    assert got == sorted(names), "walk must be exact + ordered, no dup/skip"
    assert len({i["id"] for i in items}) == 30


async def test_limit_one_walk(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    parent = await _fresh_parent(db, tree)
    names = [f"s-{i:02d}" for i in range(12)]
    await _seed_children(db, tree, parent, names)
    items = await _walk_all(client, tree, parent, sort="name", limit=1)
    assert [i["name"] for i in items] == sorted(names)


# --- created_at sort ------------------------------------------------------------


async def test_created_at_sort_full_walk(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    parent = await _fresh_parent(db, tree)
    names = [f"c-{i:02d}" for i in range(30)]
    await _seed_children(db, tree, parent, names)

    items = await _walk_all(client, tree, parent, sort="created_at", limit=7)
    got = [i["name"] for i in items]
    assert sorted(got) == sorted(names), "no dup/skip regardless of sort"
    stamps = [(i["created_at"], i["id"]) for i in items]
    assert stamps == sorted(stamps, reverse=True), "created_at DESC with id tiebreak"


# --- mid-walk insertion -----------------------------------------------------------


async def test_rows_inserted_midwalk_never_corrupt(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    """The spec's race: rows are inserted WHILE the client pages. Originals
    must appear exactly once; rows sorting before the cursor are correctly
    not picked up (keyset semantics), rows after it are."""
    parent = await _fresh_parent(db, tree)
    originals = [f"n-{i:02d}" for i in range(10)]
    await _seed_children(db, tree, parent, originals)

    page1 = await client.get(
        f"/folders/{parent.id}/children",
        headers=_owner_headers(tree),
        params={"limit": 5, "sort": "name"},  # n-00..n-04, cursor at n-04
    )
    assert page1.status_code == 200
    body = page1.json()
    assert [i["name"] for i in body["items"]] == originals[:5]
    cursor = body["next_cursor"]
    assert cursor is not None

    # Insert while "the client holds the cursor": before, inside, after.
    for name in ("a-before-cursor", "n-045-between", "z-after-cursor"):
        await seed_folder(db, tree.owner.id, parent_id=parent.id, name=name)
    await commit(db)

    rest: list[dict] = []
    for _ in range(50):
        resp = await client.get(
            f"/folders/{parent.id}/children",
            headers=_owner_headers(tree),
            params={"limit": 3, "sort": "name", "cursor": cursor},
        )
        assert resp.status_code == 200, resp.text
        b = resp.json()
        rest.extend(b["items"])
        cursor = b["next_cursor"]
        if cursor is None:
            break

    seen = [i["name"] for i in rest]
    # every original beyond the cursor exactly once, in sort order; the
    # between-row sorts BEFORE n-05 ("n-045…" < "n-05" lexicographically)
    assert seen == ["n-045-between", *originals[5:], "z-after-cursor"], seen


# --- cursor validation --------------------------------------------------------------


async def test_cursor_from_other_sort_is_422(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    parent = await _fresh_parent(db, tree)
    await _seed_children(db, tree, parent, ["x-1", "x-2", "x-3"])
    page = await client.get(
        f"/folders/{parent.id}/children",
        headers=_owner_headers(tree),
        params={"limit": 1, "sort": "name"},
    )
    name_cursor = page.json()["next_cursor"]
    resp = await client.get(
        f"/folders/{parent.id}/children",
        headers=_owner_headers(tree),
        params={"limit": 1, "sort": "created_at", "cursor": name_cursor},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "bad_cursor"


async def test_tampered_cursor_is_422(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    parent = await _fresh_parent(db, tree)
    for garbage in ("!!!not-base64!!!", "n:name-without-id", "c:2020-01-01T00:00:00"):
        resp = await client.get(
            f"/folders/{parent.id}/children",
            headers=_owner_headers(tree),
            params={"limit": 1, "sort": "name", "cursor": garbage},
        )
        assert resp.status_code == 422, f"{garbage!r}: {resp.status_code} {resp.text}"


async def test_invalid_sort_is_422(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    parent = await _fresh_parent(db, tree)
    resp = await client.get(
        f"/folders/{parent.id}/children",
        headers=_owner_headers(tree),
        params={"sort": "name; DROP TABLE folders"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "bad_filter"


# --- visibility ------------------------------------------------------------------


async def test_trashed_children_excluded(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    parent = await _fresh_parent(db, tree)
    names = [f"t-{i:02d}" for i in range(5)]
    await _seed_children(db, tree, parent, names)

    # trash one child through the API (owner = the fixture user): the walk
    # must then see exactly the four survivors
    listing = await client.get(
        f"/folders/{parent.id}/children", headers=_owner_headers(tree)
    )
    victim_id = next(
        i["id"] for i in listing.json()["items"] if i["name"] == "t-02"
    )
    resp = await client.post(
        f"/folders/{victim_id}/trash", headers=_owner_headers(tree)
    )
    assert resp.status_code == 204

    items = await _walk_all(client, tree, parent, sort="name", limit=2)
    got = [i["name"] for i in items]
    assert "t-02" not in got
    assert got == sorted(n for n in names if n != "t-02"), got


async def test_children_of_trashed_folder_still_browsable_by_owner(
    client: AsyncClient, db: AsyncSession, tree: Tree
) -> None:
    """Owner keeps read access inside a trashed subtree (restore needs it);
    the matrix covers the non-owner 404 side."""
    tree.target.deleted_at = datetime.now(UTC)
    await commit(db)
    resp = await client.get(
        f"/folders/{tree.target.id}/children", headers=_owner_headers(tree)
    )
    assert resp.status_code == 200, resp.text
    assert [i["name"] for i in resp.json()["items"]] == ["child"]


# --- root discovery ----------------------------------------------------------------


async def test_root_discovery(client: AsyncClient) -> None:
    """GET /folders/root returns the caller's own root (registered users
    get one in the registration transaction)."""
    from app.services.security import create_access_token

    email = f"root-{uuid.uuid4().hex[:10]}@example.com"
    reg = await client.post(
        "/auth/register", json={"email": email, "password": "correct-horse-1"}
    )
    assert reg.status_code == 201, reg.text
    user_id = uuid.UUID(reg.json()["id"])

    resp = await client.get(
        "/folders/root",
        headers={"Authorization": f"Bearer {create_access_token(user_id)[0]}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "My Files"
    assert body["parent_id"] is None
    assert body["owner_id"] == str(user_id)

    # A token minted for a nonexistent account → 401 from the auth guard
    # (never 404: don't confirm which user ids exist).
    ghost = create_access_token(uuid.uuid4())[0]
    resp = await client.get("/folders/root", headers={"Authorization": f"Bearer {ghost}"})
    assert resp.status_code == 401, resp.text
