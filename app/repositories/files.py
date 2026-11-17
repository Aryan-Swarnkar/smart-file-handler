"""File queries — including the keyset cursor (never OFFSET).

Cursor = (created_at, id) of the last row, newest-first. PostgreSQL row-value
comparison `(a,b) < (x,y)` matches the composite index
ix_files_user_created_id(user_id, created_at, id) and its backward scan, so
every page is an index seek regardless of depth (OFFSET degrades linearly).
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import File, FileKind, FileStatus
from app.repositories.folders import MAX_TREE_DEPTH, FolderAccessRow

# Self at depth 1, walking UP while depth < cap — the FILE variant hangs the
# chain off the file's folder_id via SUBSELECT: a NULL folder_id (top-level
# file) or a missing file row both yield an EMPTY chain, so no None bind ever
# reaches asyncpg. Mirrors FOLDER_CHAIN_SQL but stays local: the file variant
# needs file-side ownership/trash columns anyway and evolves independently.
FILE_CHAIN_SQL = """
WITH RECURSIVE chain AS (
    SELECT f.id, f.parent_id, f.owner_id, f.deleted_at, 1 AS depth
    FROM folders f
    WHERE f.id = (SELECT fi.folder_id FROM files fi WHERE fi.id = :file_id)
  UNION ALL
    SELECT p.id, p.parent_id, p.owner_id, p.deleted_at, c.depth + 1
    FROM folders p
    JOIN chain c ON p.id = c.parent_id
    WHERE c.depth < :max_depth
)
"""


class Page(NamedTuple):
    items: list[File]
    next_cursor: str | None


def encode_cursor(created_at: datetime, file_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{file_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Raises ValueError on tampered/garbage cursors — caller maps to 422."""
    padded = cursor + "=" * (-len(cursor) % 4)
    raw = base64.urlsafe_b64decode(padded.encode()).decode()
    created_iso, _, id_part = raw.rpartition("|")
    return datetime.fromisoformat(created_iso), uuid.UUID(id_part)


class FileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        original_name: str,
        mime_type: str,
        size_bytes: int,
        storage_key: str,
        status: FileStatus = FileStatus.UPLOADED,
        folder_id: uuid.UUID | None = None,
        upload_expires_at: datetime | None = None,
    ) -> File:
        """Insert one file row. Phase 5 init passes status=PENDING_UPLOAD +
        upload_expires_at (quota already reserved by the caller); completed
        uploads are backfilled by tests with the old 'uploaded' default."""
        file = File(
            user_id=user_id,
            folder_id=folder_id,
            original_name=original_name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            storage_key=storage_key,
            status=status,
            kind=None,
            sha256=None,
            upload_expires_at=upload_expires_at,
        )
        self._session.add(file)
        await self._session.flush()
        return file

    async def get_fresh(self, file_id: uuid.UUID) -> File | None:
        """Re-select and overwrite the identity-map copy — decisions must
        reflect what a concurrent transaction just committed, never a cached
        stale status (double-complete detection depends on this)."""
        stmt = (
            select(File)
            .where(File.id == file_id)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, file_id: uuid.UUID) -> File | None:
        return await self._session.get(File, file_id)

    async def list_page(
        self,
        *,
        user_id: uuid.UUID,
        limit: int,
        cursor: tuple[datetime, uuid.UUID] | None,
        kind: FileKind | None = None,
        doc_topic: str | None = None,
        status: FileStatus | None = None,
    ) -> Page:
        conditions = [File.user_id == user_id]
        if kind is not None:
            conditions.append(File.kind == kind)
        if doc_topic is not None:
            conditions.append(File.doc_topic == doc_topic)
        if status is not None:
            conditions.append(File.status == status)
        if cursor is not None:
            conditions.append(
                tuple_(File.created_at, File.id) < tuple_(cursor[0], cursor[1])
            )

        stmt = (
            select(File)
            .where(*conditions)
            .order_by(File.created_at.desc(), File.id.desc())
            .limit(limit + 1)  # +1 → has_more probe, never an extra round-trip
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        has_more = len(rows) > limit
        items: list[File] = list(rows[:limit])
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].id) if has_more else None
        )
        return Page(items=items, next_cursor=next_cursor)

    async def delete(self, file: File) -> None:
        await self._session.delete(file)
        await self._session.flush()

    async def resolve_file_role(
        self, file_id: uuid.UUID, actor_id: uuid.UUID
    ) -> FolderAccessRow:
        """Ownership + best grant (direct on the file OR on any ancestor
        folder), plus the trashed-chain flag.

        One statement on FILE_CHAIN_SQL: the chain hangs off the file's
        folder_id (subselect — NULL for a top-level file → empty chain, only
        direct grants and ownership apply). A missing file row resolves to
        role=None/chain clean; the service maps None → 404.
        """
        row = (
            await self._session.execute(
                text(
                    FILE_CHAIN_SQL
                    + """
SELECT
    (SELECT fi.user_id FROM files fi WHERE fi.id = :file_id) = :actor_id AS is_owner,
    EXISTS (
        (SELECT 1 FROM files fi
          WHERE fi.id = :file_id AND fi.deleted_at IS NOT NULL)
        UNION ALL
        (SELECT 1 FROM chain c WHERE c.deleted_at IS NOT NULL)
    ) AS chain_trashed,
    (SELECT p.role FROM permissions p
      WHERE p.grantee_id = :actor_id
        AND (p.file_id = :file_id OR p.folder_id IN (SELECT c2.id FROM chain c2))
      ORDER BY CASE p.role WHEN 'editor' THEN 0 ELSE 1 END
      LIMIT 1) AS granted_role
"""
                ),
                {
                    "file_id": file_id,
                    "actor_id": actor_id,
                    "max_depth": MAX_TREE_DEPTH,
                },
            )
        ).one()
        role: str | None = "owner" if row.is_owner else row.granted_role
        return FolderAccessRow(
            role=role, is_owner=row.is_owner, chain_trashed=row.chain_trashed
        )
