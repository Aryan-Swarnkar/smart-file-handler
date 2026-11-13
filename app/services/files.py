"""FileService: init / complete / list / get / url / rename / delete.

Business rules (user-locked, P3):
- Presigned init/complete: bytes go client→S3 directly; the API streams the
  object once at complete to hash + sniff it (25 MB ceiling keeps this cheap).
- Duplicates: identical bytes for the same user return the EXISTING row and
  the just-uploaded object is deleted (Google-Photos semantics).
- Sniff wins: declared mime is advisory; magic bytes decide kind.
- kind=other → doc_topic="other", status=done immediately (browsable now);
  processable kinds stay status=uploaded until the worker (P4) claims them.
- Ownership: any foreign/cross-user id → NotFoundError (404, never 403).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainValidationError, NotFoundError
from app.core.logging import get_logger
from app.db.models import File, FileKind, FileStatus
from app.repositories.files import FileRepository, Page, decode_cursor
from app.services.sniffing import (
    SNIFF_SAMPLE_BYTES,
    sanitize_extension,
    sanitize_filename,
    sniff,
)
from app.services.storage import StorageService

logger = get_logger(__name__)


class InitResult(NamedTuple):
    file: File
    upload_url: str
    expires_in: int


class CompleteResult(NamedTuple):
    file: File
    deduplicated: bool


class FileService:
    def __init__(self, session: AsyncSession, storage: StorageService) -> None:
        self._session = session
        self._files = FileRepository(session)
        self._storage = storage

    # --- init -----------------------------------------------------------------

    async def init_upload(
        self, user_id: uuid.UUID, filename: str, content_type: str, size: int
    ) -> InitResult:
        settings = get_settings()
        if size > settings.max_upload_bytes:
            raise DomainValidationError(
                f"File exceeds the {settings.MAX_UPLOAD_MB} MB limit.",
                code="file_too_large",
                details={"max_bytes": settings.max_upload_bytes, "declared": size},
            )
        safe_name = sanitize_filename(filename)
        ext = sanitize_extension(safe_name)
        file_id = uuid.uuid4()
        storage_key = f"{user_id}/{file_id}" + (f".{ext}" if ext else "")

        file = await self._files.create(
            user_id=user_id,
            original_name=safe_name,
            mime_type=content_type,
            size_bytes=size,
            storage_key=storage_key,
        )
        url, expires = await self._storage.presign_put(storage_key, size)
        await self._session.commit()
        logger.info(
            "file_init", user_id=str(user_id), file_id=str(file.id), size=size
        )
        return InitResult(file, url, expires)

    # --- complete ---------------------------------------------------------------

    async def complete_upload(self, user_id: uuid.UUID, file_id: uuid.UUID) -> CompleteResult:
        file = await self._files.get_by_id(file_id)
        if file is None or file.user_id != user_id:
            raise NotFoundError("File not found.")

        # Idempotent complete (at-least-once client retries): already hashed
        # means already completed — return the same result again.
        if file.sha256 is not None:
            return CompleteResult(file, deduplicated=False)

        meta = await self._storage.head(file.storage_key)
        if meta is None:
            # Client never PUT the bytes (or the presigned URL expired).
            raise ConflictError(
                "No object was uploaded for this file id.", code="upload_not_found"
            )
        actual_size = int(meta.get("ContentLength", 0))
        if actual_size != file.size_bytes:
            # Presigned PUT binds Content-Length, so this means tampering or a
            # broken client — reject rather than silently reconcile.
            raise DomainValidationError(
                "Uploaded size differs from the declared size.",
                code="size_mismatch",
                details={"declared": file.size_bytes, "actual": actual_size},
            )

        head, _ = await self._storage.read_head(file.storage_key, SNIFF_SAMPLE_BYTES)
        sniffed = sniff(file.original_name, head)

        sha256 = await self._hash_object(file.storage_key)

        # --- dedup: same user, identical bytes → keep the ORIGINAL row ------
        existing = await self._files.get_by_user_and_hash(user_id, sha256)
        if existing is not None and existing.id != file.id:
            await self._storage.delete(file.storage_key)  # the duplicate object
            await self._files.delete(file)
            await self._session.commit()
            logger.info(
                "file_deduplicated",
                user_id=str(user_id),
                file_id=str(file.id),
                kept_id=str(existing.id),
            )
            return CompleteResult(existing, deduplicated=True)

        file.sha256 = sha256
        file.kind = sniffed.kind
        file.mime_type = sniffed.mime_type
        file.size_bytes = actual_size
        if sniffed.kind == FileKind.OTHER:
            # Matrix A: unprocessable binaries are DONE and browsable now —
            # nothing for a worker to do later.
            file.doc_topic = "other"
            file.status = FileStatus.DONE
        else:
            file.status = FileStatus.UPLOADED  # P4 queue claims it from here
        await self._session.commit()
        logger.info(
            "file_completed",
            user_id=str(user_id),
            file_id=str(file.id),
            kind=sniffed.kind.value,
            sha256=sha256[:12],
        )
        return CompleteResult(file, deduplicated=False)

    async def _hash_object(self, key: str) -> str:
        """Streaming SHA-256 — constant memory regardless of object size."""
        digest = hashlib.sha256()
        async for chunk in self._storage.read(key):
            digest.update(chunk)
        return digest.hexdigest()

    # --- browse ---------------------------------------------------------------

    async def list_files(
        self,
        user_id: uuid.UUID,
        *,
        limit: int,
        cursor: str | None,
        kind: FileKind | None = None,
        doc_topic: str | None = None,
        status: FileStatus | None = None,
    ) -> Page:
        cursor_tuple: tuple[datetime, uuid.UUID] | None = None
        if cursor:
            try:
                cursor_tuple = decode_cursor(cursor)
            except (ValueError, UnicodeDecodeError) as exc:
                raise DomainValidationError(
                    "Malformed pagination cursor.", code="bad_cursor"
                ) from exc
        return await self._files.list_page(
            user_id=user_id,
            limit=limit,
            cursor=cursor_tuple,
            kind=kind,
            doc_topic=doc_topic,
            status=status,
        )

    async def get_file(self, user_id: uuid.UUID, file_id: uuid.UUID) -> File:
        file = await self._files.get_by_id(file_id)
        if file is None or file.user_id != user_id:
            raise NotFoundError("File not found.")
        return file

    async def get_view_url(
        self, user_id: uuid.UUID, file_id: uuid.UUID, download: bool
    ) -> tuple[File, str, int]:
        file = await self.get_file(user_id, file_id)
        url, expires = await self._storage.presign_get(file.storage_key, download)
        return file, url, expires

    # --- mutate -----------------------------------------------------------------

    async def rename(self, user_id: uuid.UUID, file_id: uuid.UUID, new_name: str) -> File:
        file = await self.get_file(user_id, file_id)
        file.original_name = sanitize_filename(new_name)
        await self._session.commit()
        logger.info("file_renamed", user_id=str(user_id), file_id=str(file_id))
        return file

    async def delete_file(self, user_id: uuid.UUID, file_id: uuid.UUID) -> None:
        file = await self.get_file(user_id, file_id)
        # S3 first: if storage errors, the row survives and the client can
        # retry (S3 delete is idempotent). DB last: a committed row must never
        # point at an object we failed to delete.
        await self._storage.delete(file.storage_key)
        await self._files.delete(file)
        await self._session.commit()
        logger.info("file_deleted", user_id=str(user_id), file_id=str(file_id))
