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
from app.repositories.folders import FOLDER_CHAIN_SQL, MAX_TREE_DEPTH, FolderAccessRow


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
    ) -> File:
        file = File(
            user_id=user_id,
            original_name=original_name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            storage_key=storage_key,
            status=FileStatus.UPLOADED,
            kind=None,
            sha256=None,
        )
        self._session.add(file)
        await self._session.flush()
        return file

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

        Self-contained statement (the folder chain CTE mirrors
        repositories.folders — deliberate: each repo stays independently
        readable, and the file variant needs file-side ownership + trash
        columns anyway). The chain hangs off the file's folder_id; a file at
        the drive top level has folder_id NULL → empty chain, only direct
        grants and ownership apply.
        """
        row = (
            await self._session.execute(
                text(
                    FOLDER_CHAIN_SQL
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
