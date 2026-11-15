"""Folder tree queries.

Adjacency list + recursive CTE (depth cap 20 — docs/adr/0001). ALL folder SQL
lives here. Phase 4 adds: ancestor-chain role resolution (the AccessService
engine), children keyset pages (name ASC / created_at DESC), node level +
subtree height (the depth cap), trash-state probes, and the per-owner
advisory lock that serializes moves.

Depth convention: the ROOT folder is level 1; no root-to-leaf path may exceed
MAX_TREE_DEPTH nodes. The CTE caps chain rows at MAX_TREE_DEPTH exactly like
ADR 0001's example (self at depth 1, walk parents while depth < cap), so a
legal tree always fits inside one CTE evaluation.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime
from typing import Literal, NamedTuple

from sqlalchemy import select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Folder

MAX_TREE_DEPTH = 20

SortKey = Literal["name", "created_at"]


class FolderPage(NamedTuple):
    items: list[Folder]
    next_cursor: str | None


class FolderCursor(NamedTuple):
    """Decoded keyset position. Exactly one of name/created_at is set,
    matching the requested sort; folder_id is the tie-break."""

    name: str | None
    created_at: datetime | None
    folder_id: uuid.UUID


class FolderAccessRow(NamedTuple):
    """Raw resolve result — AccessService turns it into an AccessRole."""

    role: str | None  # highest granted role ('editor'/'viewer'), or None
    is_owner: bool
    chain_trashed: bool  # self or any ancestor carries deleted_at


# Self at depth 1, walking UP while depth < cap → at most 20 rows (self + 19
# ancestors), which is exactly a maximal legal chain (root at level 20).
# Public: repositories/files.py reuses it for the file-target resolve.
FOLDER_CHAIN_SQL = """
WITH RECURSIVE chain AS (
    SELECT f.id, f.parent_id, f.owner_id, f.deleted_at, 1 AS depth
    FROM folders f
    WHERE f.id = :folder_id
  UNION ALL
    SELECT p.id, p.parent_id, p.owner_id, p.deleted_at, c.depth + 1
    FROM folders p
    JOIN chain c ON p.id = c.parent_id
    WHERE c.depth < :max_depth
)
"""

_DOWN_SQL = """
WITH RECURSIVE down AS (
    SELECT f.id, 1 AS height
    FROM folders f
    WHERE f.id = :folder_id
  UNION ALL
    SELECT c.id, d.height + 1
    FROM folders c
    JOIN down d ON c.parent_id = d.id
    WHERE d.height < :max_depth
)
"""

# Grants rank editor above viewer; the resource itself is part of the chain
# (depth 1), so "direct grant" and "grant on an ancestor" are one lookup.
_GRANTED_ROLE_SQL = """
    (SELECT p.role FROM permissions p
      WHERE p.grantee_id = :actor_id
        AND p.folder_id IN (SELECT c2.id FROM chain c2)
      ORDER BY CASE p.role WHEN 'editor' THEN 0 ELSE 1 END
      LIMIT 1) AS granted_role
"""


def advisory_key(owner_id: uuid.UUID) -> int:
    """Stable 64-bit advisory-lock key derived from the owner's UUID.

    First 8 bytes of the UUID, big-endian. Computed in Python (no SQL text
    casting games); two users share a key only on a 128→64-bit collision,
    which would just serialize unrelated moves briefly — never a correctness
    issue.
    """
    return int.from_bytes(owner_id.bytes[:8], "big", signed=True)


# --- cursor ---------------------------------------------------------------------
# Payload "n:<name>|<id>" (name ASC) or "c:<iso8601>|<id>" (created_at DESC).
# The trailing id is the tie-break; rpartition splits at the LAST "|", so
# names containing "|" survive. The n:/c: prefix is checked against the
# requested sort — a cursor from the other sort must not silently reuse.


def encode_folder_cursor(sort: SortKey, value: str, folder_id: uuid.UUID) -> str:
    """`value` is the name itself (name sort) or an ISO timestamp (created_at
    sort) — stringly typed so this stays free of branchy narrowing."""
    raw = f"{'n' if sort == 'name' else 'c'}:{value}|{folder_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_folder_cursor(cursor: str, sort: SortKey) -> FolderCursor:
    """Raises ValueError on garbage, tampered, or sort-mismatched cursors —
    the service maps that to 422 bad_cursor."""
    padded = cursor + "=" * (-len(cursor) % 4)
    raw = base64.urlsafe_b64decode(padded.encode()).decode()
    head, _, id_part = raw.rpartition("|")
    prefix, _, value = head.partition(":")
    folder_id = uuid.UUID(id_part)
    if sort == "name" and prefix == "n":
        return FolderCursor(name=value, created_at=None, folder_id=folder_id)
    if sort == "created_at" and prefix == "c":
        return FolderCursor(
            name=None, created_at=datetime.fromisoformat(value), folder_id=folder_id
        )
    raise ValueError("cursor does not match the requested sort")


class FolderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, owner_id: uuid.UUID, name: str, *, parent_id: uuid.UUID | None = None
    ) -> Folder:
        """Insert one folder (flush, not commit — the service owns the
        transaction boundary; registration relies on that to make user +
        root folder atomic)."""
        folder = Folder(owner_id=owner_id, parent_id=parent_id, name=name)
        self._session.add(folder)
        await self._session.flush()
        return folder

    # --- reads ----------------------------------------------------------------

    async def get_fresh(self, folder_id: uuid.UUID) -> Folder | None:
        """Re-select the row and refresh the identity-map instance.

        A plain session.get() returns the CACHED object without reloading
        attributes — fatal for move/trash decisions that must reflect what a
        concurrent transaction just committed. populate_existing forces the
        SELECT to overwrite loaded state.
        """
        stmt = (
            select(Folder)
            .where(Folder.id == folder_id)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_owner_id(self, folder_id: uuid.UUID) -> uuid.UUID | None:
        """Column-level read: existence + owner, bypassing the identity map.

        Move needs the owner BEFORE it can take the advisory lock; a raw
        column SELECT is also the cheapest existence probe (404 fast-path).
        """
        return (
            await self._session.execute(
                select(Folder.owner_id).where(Folder.id == folder_id)
            )
        ).scalar_one_or_none()

    async def get_root_for_owner(self, owner_id: uuid.UUID) -> Folder | None:
        """The user's root folder (parent_id IS NULL). Unique per user by
        construction (registration backfill); LIMIT 1 on the partial index
        is exact."""
        stmt = (
            select(Folder)
            .where(Folder.owner_id == owner_id, Folder.parent_id.is_(None))
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    # --- role resolution (AccessService's engine) -----------------------------

    async def resolve_folder_role(
        self, folder_id: uuid.UUID, actor_id: uuid.UUID
    ) -> FolderAccessRow:
        """Ownership + best grant on the self-and-ancestors chain,
        trashed-chain flag.

        One statement: the CTE is evaluated once and reused by all three
        probes. Raw SQL (not ORM) because the recursive CTE must see the rows
        as committed RIGHT NOW, never the identity map's cached version.
        """
        row = (
            await self._session.execute(
                text(
                    FOLDER_CHAIN_SQL
                    + """
SELECT
    (SELECT c.owner_id FROM chain c WHERE c.depth = 1) = :actor_id AS is_owner,
    EXISTS (SELECT 1 FROM chain c WHERE c.deleted_at IS NOT NULL) AS chain_trashed,
    """
                    + _GRANTED_ROLE_SQL
                ),
                {
                    "folder_id": folder_id,
                    "actor_id": actor_id,
                    "max_depth": MAX_TREE_DEPTH,
                },
            )
        ).one()
        role: str | None = "owner" if row.is_owner else row.granted_role
        return FolderAccessRow(
            role=role, is_owner=row.is_owner, chain_trashed=row.chain_trashed
        )

    # --- tree geometry (depth cap + cycle prevention) --------------------------

    async def node_level(self, folder_id: uuid.UUID) -> int:
        """Level of the node (root = 1) = number of nodes on the root→node
        path = max chain depth. Bounded by the CTE cap; the service refuses
        trees beyond the cap, so the cap never truncates a legal path."""
        level = (
            await self._session.execute(
                text(FOLDER_CHAIN_SQL + "SELECT max(depth) AS level FROM chain"),
                {
                    "folder_id": folder_id,
                    "max_depth": MAX_TREE_DEPTH,
                },
            )
        ).scalar_one()
        return int(level)

    async def subtree_height(self, folder_id: uuid.UUID) -> int:
        """Height of the subtree rooted at the node (single node → 1)."""
        height = (
            await self._session.execute(
                text(_DOWN_SQL + "SELECT max(height) AS height FROM down"),
                {
                    "folder_id": folder_id,
                    "max_depth": MAX_TREE_DEPTH,
                },
            )
        ).scalar_one()
        return int(height)

    async def subtree_contains(
        self, folder_id: uuid.UUID, candidate_id: uuid.UUID
    ) -> bool:
        """Is `candidate_id` inside the subtree rooted at `folder_id`?

        The root row of `down` is the folder itself, so candidate == folder
        is True — one rule covers move-into-self and move-into-descendant.
        """
        exists = (
            await self._session.execute(
                text(
                    _DOWN_SQL
                    + "SELECT EXISTS (SELECT 1 FROM down WHERE id = :candidate_id)"
                ),
                {
                    "folder_id": folder_id,
                    "candidate_id": candidate_id,
                    "max_depth": MAX_TREE_DEPTH,
                },
            )
        ).scalar_one()
        return bool(exists)

    # --- trash-state probes -----------------------------------------------------

    async def trashed_in_chain(self, folder_id: uuid.UUID) -> bool:
        """Self or any ancestor trashed — the mutation gate for live work."""
        trashed = (
            await self._session.execute(
                text(
                    FOLDER_CHAIN_SQL
                    + "SELECT EXISTS ("
                    "SELECT 1 FROM chain c WHERE c.deleted_at IS NOT NULL)"
                ),
                {"folder_id": folder_id, "max_depth": MAX_TREE_DEPTH},
            )
        ).scalar_one()
        return bool(trashed)

    async def ancestor_trashed(self, folder_id: uuid.UUID) -> bool:
        """Any ANCESTOR (not the node itself) trashed — restore precondition."""
        trashed = (
            await self._session.execute(
                text(
                    FOLDER_CHAIN_SQL
                    + "SELECT EXISTS ("
                    "SELECT 1 FROM chain c WHERE c.depth > 1 "
                    "AND c.deleted_at IS NOT NULL)"
                ),
                {"folder_id": folder_id, "max_depth": MAX_TREE_DEPTH},
            )
        ).scalar_one()
        return bool(trashed)

    # --- move -----------------------------------------------------------------

    async def lock_owner_tree(self, owner_id: uuid.UUID) -> None:
        """pg_advisory_xact_lock on the owner: serializes every concurrent
        move of one owner's tree until this transaction ends. The move's
        subtree check runs AFTER this returns, so it always sees whatever a
        competing move committed — a cycle can never be written."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": advisory_key(owner_id)},
        )

    # --- children listing (keyset, never OFFSET) --------------------------------

    async def list_children_page(
        self,
        *,
        folder_id: uuid.UUID,
        sort: SortKey,
        limit: int,
        cursor: FolderCursor | None,
    ) -> FolderPage:
        """Live children only (deleted_at IS NULL — the partial index's
        predicate), keyset-paginated. name sort walks A→Z with an (name, id)
        ascending seek; created_at sort walks newest-first with a descending
        seek, mirroring the files listing. limit+1 rows probe has_more
        without a second round-trip."""
        conditions = [Folder.parent_id == folder_id, Folder.deleted_at.is_(None)]
        if sort == "name":
            if cursor is not None:
                if cursor.name is None:
                    raise ValueError("name-sort cursor without a name component")
                conditions.append(
                    tuple_(Folder.name, Folder.id)
                    > tuple_(cursor.name, cursor.folder_id)
                )
        else:
            if cursor is not None:
                if cursor.created_at is None:
                    raise ValueError("created_at cursor without a timestamp")
                conditions.append(
                    tuple_(Folder.created_at, Folder.id)
                    < tuple_(cursor.created_at, cursor.folder_id)
                )

        stmt = select(Folder).where(*conditions)
        if sort == "name":
            stmt = stmt.order_by(Folder.name.asc(), Folder.id.asc())
        else:
            stmt = stmt.order_by(Folder.created_at.desc(), Folder.id.desc())

        rows = (
            (await self._session.execute(stmt.limit(limit + 1))).scalars().all()
        )
        has_more = len(rows) > limit
        items: list[Folder] = list(rows[:limit])
        next_cursor: str | None = None
        if has_more and items:
            last = items[-1]
            value = last.name if sort == "name" else last.created_at.isoformat()
            next_cursor = encode_folder_cursor(sort, value, last.id)
        return FolderPage(items=items, next_cursor=next_cursor)
