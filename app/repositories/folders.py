"""Folder tree queries.

Adjacency list + recursive CTE (depth cap 20) for ancestor walks and role
inheritance; move-cycle checks; keyset pagination by name or created_at.
ALL folder SQL lives here (Phase 4 adds the tree queries; Phase 3 needs
only the creation path).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Folder


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
