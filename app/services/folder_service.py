"""FolderService: create / get / list children / move / trash / restore.

Business rules (PHASE 4, decisions locked with the user):
- Every method takes an explicit actor and calls AccessService first; any
  denial is NotFoundError (404, never 403).
- Children listing is FOLDERS ONLY, keyset-paginated by name (default) or
  created_at — never OFFSET.
- Move: destination requires EDITOR (placing = a write to the destination's
  children), cross-owner moves are forbidden (trees never merge — inherited
  grants and listings stay leak-free by construction), moving into the
  folder's own subtree is rejected, and everything runs under a
  pg_advisory_xact_lock on the owner taken BEFORE the fresh re-reads, so
  concurrent opposing moves cannot create cycles.
- Depth cap 20 (docs/adr/0001): creates and moves that would push any
  root→leaf path past 20 nodes are rejected.
- Trash = deleted_at on the top-level item only (descendants die via the
  trashed-ancestor rule, not their own flags). Trash/restore are
  owner-only and idempotent. Restore fails while an ancestor is still
  trashed (restore top-down). The ROOT folder cannot be trashed.
- Live work inside a trashed chain is 422 (code "trashed"); read/list stay
  allowed for the owner.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainValidationError, NotFoundError
from app.core.logging import get_logger
from app.db.models import Folder
from app.repositories.folders import (
    MAX_TREE_DEPTH,
    FolderCursor,
    FolderPage,
    FolderRepository,
    decode_folder_cursor,
)
from app.services.access_service import AccessRole, AccessService, role_meets

logger = get_logger(__name__)

SortParam = Literal["name", "created_at"]


class FolderService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._folders = FolderRepository(session)
        self._access = AccessService(session)

    # --- internal gates ---------------------------------------------------------

    async def _require_mutable(
        self, actor_id: uuid.UUID, folder_id: uuid.UUID, minimum: AccessRole
    ) -> AccessRole:
        """Access ≥ minimum on a LIVE chain — the gate for every mutation
        other than trash/restore themselves.

        resolve_folder_access applies the trash rule: non-owners resolve to
        None on a trashed chain (→ 404 below); reaching the trashed branch
        therefore means the actor is the OWNER — a state conflict (422),
        never an access denial.
        """
        role, chain_trashed = await self._access.resolve_folder_access(
            actor_id, folder_id
        )
        if not role_meets(role, minimum):
            raise NotFoundError("Folder not found.")
        if chain_trashed:
            raise DomainValidationError(
                "Folder is in the trash (restore it first).", code="trashed"
            )
        return role

    async def _get_visible(
        self, actor_id: uuid.UUID, folder_id: uuid.UUID
    ) -> Folder:
        """Fresh row + viewer-or-better. The owner passes even when the
        folder (or an ancestor) is trashed — trash stays browsable to its
        owner; everyone else 404s."""
        folder = await self._folders.get_fresh(folder_id)
        if folder is None:
            raise NotFoundError("Folder not found.")
        await self._access.require_role(actor_id, folder, AccessRole.VIEWER)
        return folder

    # --- create -----------------------------------------------------------------

    async def create(
        self, actor_id: uuid.UUID, name: str, parent_id: uuid.UUID
    ) -> Folder:
        """New folder inside an editable parent. parent_id is REQUIRED —
        every user already owns exactly one root ("My Files", created in the
        registration transaction); a second root would break the
        single-root invariant the Phase 3 migration backfills."""
        name = name.strip()
        if not name:
            raise DomainValidationError(
                "Folder name cannot be empty.", code="empty_name"
            )
        parent = await self._folders.get_fresh(parent_id)
        if parent is None:
            raise NotFoundError("Parent folder not found.")
        await self._require_mutable(actor_id, parent_id, AccessRole.EDITOR)
        if await self._folders.node_level(parent_id) + 1 > MAX_TREE_DEPTH:
            raise DomainValidationError(
                f"Folders cannot be nested deeper than {MAX_TREE_DEPTH} levels.",
                code="depth_limit",
            )
        # The new folder belongs to the PARENT's owner — creating inside a
        # folder shared with you as editor grows that owner's tree (Drive
        # semantics; Phase 5 charges that owner's quota for uploads too).
        folder = await self._folders.create(
            owner_id=parent.owner_id, name=name, parent_id=parent_id
        )
        await self._session.commit()
        logger.info(
            "folder_created",
            actor_id=str(actor_id),
            folder_id=str(folder.id),
            parent_id=str(parent_id),
        )
        return folder

    # --- read -------------------------------------------------------------------

    async def get(self, actor_id: uuid.UUID, folder_id: uuid.UUID) -> Folder:
        return await self._get_visible(actor_id, folder_id)

    async def rename(
        self, actor_id: uuid.UUID, folder_id: uuid.UUID, new_name: str
    ) -> Folder:
        """Rename requires EDITOR. The root folder renames too — it is an
        ordinary row; nothing in the code depends on the literal "My Files"
        after registration."""
        new_name = new_name.strip()
        if not new_name:
            raise DomainValidationError(
                "Folder name cannot be empty.", code="empty_name"
            )
        await self._require_mutable(actor_id, folder_id, AccessRole.EDITOR)
        folder = await self._folders.get_fresh(folder_id)
        if folder is None:  # deleted between gate and read — vanishingly rare
            raise NotFoundError("Folder not found.")
        folder.name = new_name
        await self._session.commit()
        logger.info(
            "folder_renamed", actor_id=str(actor_id), folder_id=str(folder_id)
        )
        return folder

    async def get_root(self, actor_id: uuid.UUID) -> Folder:
        """The actor's own root folder — parent_id IS NULL for their id.

        Discovery endpoint: a fresh client has no other way to learn the id
        it needs as parent_id for every subsequent create.
        """
        root = await self._folders.get_root_for_owner(actor_id)
        if root is None:
            # Only reachable if registration was interrupted mid-backfill.
            raise NotFoundError("Root folder not found.")
        await self._access.require_role(actor_id, root, AccessRole.VIEWER)
        return root

    # --- list children ------------------------------------------------------------

    async def list_children(
        self,
        actor_id: uuid.UUID,
        folder_id: uuid.UUID,
        *,
        limit: int,
        cursor: str | None,
        sort: SortParam,
    ) -> FolderPage:
        await self._get_visible(actor_id, folder_id)
        decoded: FolderCursor | None = None
        if cursor:
            try:
                decoded = decode_folder_cursor(cursor, sort)
            except (ValueError, UnicodeDecodeError) as exc:
                raise DomainValidationError(
                    "Malformed pagination cursor.", code="bad_cursor"
                ) from exc
        return await self._folders.list_children_page(
            folder_id=folder_id,
            sort=sort,
            limit=limit,
            cursor=decoded,
        )

    # --- move ----------------------------------------------------------------------

    async def move(
        self, actor_id: uuid.UUID, folder_id: uuid.UUID, new_parent_id: uuid.UUID
    ) -> Folder:
        """Reparent one folder. Correctness under concurrency:

        1. existence probe (fresh column read) → 404 without touching locks
        2. pg_advisory_xact_lock keyed on the OWNER — every competing move
           of this owner's tree serializes here
        3. fresh re-read of folder + destination (populate_existing — the
           identity map must not answer with pre-lock state)
        4. authz + trashed + cross-owner + subtree + depth checks
        5. UPDATE parent_id, commit (lock releases with the transaction)

        The check-after-lock order is what makes the concurrent-move test
        deterministic: the loser of the race re-reads the winner's commit
        and rejects itself instead of writing a cycle.
        """
        owner_id = await self._folders.get_owner_id(folder_id)
        if owner_id is None:
            raise NotFoundError("Folder not found.")

        await self._folders.lock_owner_tree(owner_id)

        folder = await self._folders.get_fresh(folder_id)
        if folder is None:  # deleted between probe and lock (purge, later phase)
            raise NotFoundError("Folder not found.")
        # The trash rule is applied inside resolve_folder_access: non-owners
        # on a trashed chain resolve to None → 404; the trashed branch below
        # is owner-only (state conflict, not an access denial).
        role, chain_trashed = await self._access.resolve_folder_access(
            actor_id, folder_id
        )
        if not role_meets(role, AccessRole.EDITOR):
            raise NotFoundError("Folder not found.")
        if chain_trashed:
            raise DomainValidationError(
                "Folder is in the trash (restore it first).", code="trashed"
            )

        dest = await self._folders.get_fresh(new_parent_id)
        if dest is None:
            raise NotFoundError("Destination folder not found.")
        dest_role, dest_trashed = await self._access.resolve_folder_access(
            actor_id, new_parent_id
        )
        if not role_meets(dest_role, AccessRole.EDITOR):
            raise NotFoundError("Destination folder not found.")
        if dest_trashed:
            raise DomainValidationError(
                "Destination folder is in the trash.", code="destination_trashed"
            )
        if dest.owner_id != folder.owner_id:
            raise DomainValidationError(
                "Folders cannot be moved across owners.", code="cross_owner_move"
            )
        if await self._folders.subtree_contains(folder_id, new_parent_id):
            raise DomainValidationError(
                "A folder cannot be moved into its own subtree.",
                code="folder_cycle",
            )
        if (
            await self._folders.node_level(new_parent_id)
            + await self._folders.subtree_height(folder_id)
            > MAX_TREE_DEPTH
        ):
            raise DomainValidationError(
                f"Folders cannot be nested deeper than {MAX_TREE_DEPTH} levels.",
                code="depth_limit",
            )

        folder.parent_id = new_parent_id
        await self._session.commit()
        logger.info(
            "folder_moved",
            actor_id=str(actor_id),
            folder_id=str(folder_id),
            new_parent_id=str(new_parent_id),
        )
        return folder

    # --- trash / restore --------------------------------------------------------

    async def trash(self, actor_id: uuid.UUID, folder_id: uuid.UUID) -> None:
        """Soft-delete the top-level item only (deleted_at on THIS row).
        Descendants keep deleted_at NULL — the trashed-ancestor rule in
        AccessService is what hides them. Idempotent; root is not trashable."""
        folder = await self._folders.get_fresh(folder_id)
        if folder is None:
            raise NotFoundError("Folder not found.")
        role, chain_trashed = await self._access.resolve_folder_access(
            actor_id, folder_id
        )
        if not role_meets(role, AccessRole.OWNER):
            raise NotFoundError("Folder not found.")
        if folder.deleted_at is not None:
            return  # already trashed — idempotent no-op
        if folder.parent_id is None:
            raise DomainValidationError(
                "The root folder cannot be trashed.", code="root_trash"
            )
        if chain_trashed:  # an ANCESTOR is trashed (self is live, checked above)
            raise DomainValidationError(
                "Restore the trashed ancestor folder first.", code="trashed"
            )
        folder.deleted_at = datetime.now(UTC)
        await self._session.commit()
        logger.info("folder_trashed", actor_id=str(actor_id), folder_id=str(folder_id))

    async def restore(self, actor_id: uuid.UUID, folder_id: uuid.UUID) -> None:
        """Clear deleted_at. Idempotent; refuses while an ancestor is still
        trashed — restore top-down or the folder comes back invisible."""
        folder = await self._folders.get_fresh(folder_id)
        if folder is None:
            raise NotFoundError("Folder not found.")
        role, _ = await self._access.resolve_folder_access(actor_id, folder_id)
        if not role_meets(role, AccessRole.OWNER):
            raise NotFoundError("Folder not found.")
        if folder.deleted_at is None:
            return  # not trashed — idempotent no-op
        if await self._folders.ancestor_trashed(folder_id):
            raise DomainValidationError(
                "Restore the trashed ancestor folder first.", code="parent_trashed"
            )
        folder.deleted_at = None
        await self._session.commit()
        logger.info(
            "folder_restored", actor_id=str(actor_id), folder_id=str(folder_id)
        )
