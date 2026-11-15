"""Direct permission (viewer | editor) queries on files and folders.

ALL permission SQL lives here. Phase 4 needs the write path (the authz
matrix plants grants directly — grant/revocation ENDPOINTS are Phase 8);
the read side of authorization lives in the resolve queries (folders.py /
files.py), which join permissions inside the ancestor-chain CTE.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Permission, PermissionRole


class PermissionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_grant(
        self,
        *,
        grantee_id: uuid.UUID,
        role: PermissionRole,
        folder_id: uuid.UUID | None = None,
        file_id: uuid.UUID | None = None,
        granted_by_id: uuid.UUID | None = None,
    ) -> Permission:
        """Insert one grant row (flush, not commit — the caller owns the
        transaction). Exactly one target must be set; the DB CHECK
        ck_permissions_exactly_one_target is the backstop."""
        grant = Permission(
            grantee_id=grantee_id,
            role=role,
            folder_id=folder_id,
            file_id=file_id,
            granted_by_id=granted_by_id,
        )
        self._session.add(grant)
        await self._session.flush()
        return grant
