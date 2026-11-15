"""AccessService.resolve_role — the ONLY authorization gate.

Spec (PHASE 4): every service method takes an explicit actor and calls this
first; no access raises NotFound (404, never 403 — a 403 would confirm the
resource exists to someone who must not even see it). The role is the
HIGHEST of: ownership, a direct grant on the resource, or a grant on any
ancestor folder (recursive CTE, depth cap 20). A trashed ancestor makes the
subtree inaccessible except to the owner: non-owners resolve to None even
with a valid grant, while the owner keeps full access (the trash is theirs
to restore).

All SQL lives in repositories; this module is pure orchestration + the role
lattice. Phase 4.
"""

from __future__ import annotations

import enum
import uuid
from typing import TypeGuard

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import File, Folder
from app.repositories.files import FileRepository
from app.repositories.folders import FolderAccessRow, FolderRepository


class AccessRole(enum.StrEnum):
    """Effective access level. DB rows only ever store viewer|editor
    (PermissionRole) — owner is RESOLVED from ownership, never granted."""

    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


_RANK: dict[AccessRole, int] = {
    AccessRole.VIEWER: 0,
    AccessRole.EDITOR: 1,
    AccessRole.OWNER: 2,
}


def role_meets(role: AccessRole | None, minimum: AccessRole) -> TypeGuard[AccessRole]:
    """Does the resolved role reach the required minimum on the lattice
    viewer < editor < owner? None (no access) never qualifies. The TypeGuard
    lets callers `if not role_meets(role, m): raise` and keep using `role`
    as a plain AccessRole afterwards."""
    return role is not None and _RANK[role] >= _RANK[minimum]


class AccessService:
    def __init__(self, session: AsyncSession) -> None:
        self._folders = FolderRepository(session)
        self._files = FileRepository(session)

    async def resolve_role(
        self, actor_id: uuid.UUID, resource: Folder | File
    ) -> AccessRole | None:
        """Effective role of actor on the resource, or None = no access.

        Takes the ORM row (the spec's `resource`), queries by its id — so the
        decision always reflects the row AS COMMITTED RIGHT NOW, never a
        possibly-stale identity-map copy.
        """
        if isinstance(resource, Folder):
            return await self.resolve_folder_role(actor_id, resource.id)
        if isinstance(resource, File):
            row = await self._files.resolve_file_role(resource.id, actor_id)
            return self._effective(row)
        else:  # pragma: no cover — type system keeps callers honest
            raise TypeError(f"Unsupported resource type: {type(resource)!r}")

    async def resolve_folder_role(
        self, actor_id: uuid.UUID, folder_id: uuid.UUID
    ) -> AccessRole | None:
        row = await self._folders.resolve_folder_role(folder_id, actor_id)
        return self._effective(row)

    async def resolve_folder_access(
        self, actor_id: uuid.UUID, folder_id: uuid.UUID
    ) -> tuple[AccessRole | None, bool]:
        """EFFECTIVE role (trash rule applied) + trashed-chain flag in one
        query. The flag gates the OWNER's mutations on a trashed subtree
        (422 in the service); non-owners already get None here (→ 404)
        because a trashed subtree is invisible to them.
        """
        row = await self._folders.resolve_folder_role(folder_id, actor_id)
        return self._effective(row), row.chain_trashed

    async def require_role(
        self,
        actor_id: uuid.UUID,
        resource: Folder | File,
        minimum: AccessRole,
        *,
        message: str = "Resource not found.",
    ) -> AccessRole:
        """resolve + enforce in one step; denial is ALWAYS 404."""
        role = await self.resolve_role(actor_id, resource)
        if not role_meets(role, minimum):
            raise NotFoundError(message)
        return role

    @staticmethod
    def _effective(row: FolderAccessRow) -> AccessRole | None:
        """The trash rule, applied exactly once for every caller: the owner
        keeps the role on a trashed chain (trash is theirs to manage);
        editors/viewers/strangers lose it entirely (→ 404 upstream)."""
        if row.role is None:
            return None
        if row.chain_trashed and row.role != "owner":
            return None
        return AccessRole(row.role)
