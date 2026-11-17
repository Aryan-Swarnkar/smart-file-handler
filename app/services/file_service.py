"""FileService: init / complete / list / get / url / rename / delete.

Phase 5 business rules (locked with the user):
- Every method takes an explicit actor and resolves access through
  AccessService first; denial is 404, never 403. Files belong to the folder
  OWNER (user_id = owner, quota charged there) — editors/viewers reach a
  file through grants on its ancestor folder via the file-role resolver.
- init: optional folder_id (absent = the caller's own root); size ≤ 25 MB;
  one conditional quota UPDATE reserves the owner's bytes (409
  quota_exceeded on miss); the row lands as pending_upload with
  upload_expires_at = now+UPLOAD_TTL; presigned PUT goes client→S3.
- Idempotency-Key on init: same key+body replays the stored 201 verbatim;
  same key+different body is 422; parallel same-key inits serialize on a
  per-(user,key) advisory lock. Failures roll back the whole tx (no row →
  retry re-executes honestly).
- complete: HEAD → missing object 409 (reservation kept — the client may
  still PUT via the live URL; the sweeper reaps abandoned rows); size
  mismatch → DELETE object + release quota + 409; svg/html bytes → DELETE +
  release + 422 rejected_type (dangerous markup never gets stored);
  otherwise sniff wins over the declared type. The transition to
  `uploaded` + the file.uploaded outbox row happen in ONE transaction,
  guarded by a conditional UPDATE WHERE status='pending_upload' —
  concurrent completers race to exactly one winner (one outbox row), the
  losers no-op. 202. No Celery enqueue (relay = Phase 6).
- delete: S3 first (idempotent), then release + row delete in one tx.
- Behavior change vs Phase 2 (documented in PROGRESS.md): kind=other no
  longer completes to DONE inline — everything transitions to `uploaded`
  and the worker (Phase 6/7) finishes it; complete is 202, not 200.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple, cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainValidationError, NotFoundError
from app.core.logging import get_logger, request_id_var
from app.db.models import File, FileKind, FileStatus
from app.repositories.files import FileRepository, Page, decode_cursor
from app.repositories.folders import FolderRepository
from app.repositories.idempotency import IdempotencyRepository, request_hash
from app.repositories.outbox import OutboxRepository
from app.repositories.quota import QuotaRepository
from app.schemas.files import FileInitOut
from app.services.access_service import AccessRole, AccessService, role_meets
from app.services.sniffing import (
    SNIFF_SAMPLE_BYTES,
    is_rejected_markup,
    sanitize_extension,
    sanitize_filename,
    sniff,
)
from app.services.storage_service import StorageService

logger = get_logger(__name__)


class InitOutcome(NamedTuple):
    """One init attempt. `replay_body` non-None → the router replays the
    stored response verbatim (201) and ignores the other fields."""

    file: File | None
    upload_url: str
    expires_in: int
    replay_body: dict[str, Any] | None


class CompleteOutcome(NamedTuple):
    """One complete attempt. `transitioned` True = THIS request moved the row
    pending_upload → uploaded (202 Accepted — worker picks it up later);
    False = a double/replayed complete of an already-uploaded row (200 +
    current state — the locked idempotency contract)."""

    file: File
    transitioned: bool


class FileService:
    def __init__(self, session: AsyncSession, storage: StorageService) -> None:
        self._session = session
        self._files = FileRepository(session)
        self._folders = FolderRepository(session)
        self._quota = QuotaRepository(session)
        self._idem = IdempotencyRepository(session)
        self._outbox = OutboxRepository(session)
        self._access = AccessService(session)
        self._storage = storage

    # --- init -----------------------------------------------------------------

    async def init_upload(
        self,
        actor_id: uuid.UUID,
        filename: str,
        content_type: str,
        size: int,
        *,
        folder_id: uuid.UUID | None = None,
        idempotency_key: str | None = None,
    ) -> InitOutcome:
        settings = get_settings()
        if size > settings.max_upload_bytes:
            raise DomainValidationError(
                f"File exceeds the {settings.MAX_UPLOAD_MB} MB limit.",
                code="file_too_large",
                details={"max_bytes": settings.max_upload_bytes, "declared": size},
            )

        body_hash = request_hash(
            filename, content_type, size, str(folder_id) if folder_id else ""
        )

        if idempotency_key:
            await self._idem.lock(actor_id, idempotency_key)
            existing = await self._idem.get_for_replay(actor_id, idempotency_key)
            if existing is not None:
                if existing.request_hash != body_hash:
                    raise DomainValidationError(
                        "This Idempotency-Key was already used with a different "
                        "request body.",
                        code="idempotency_body_mismatch",
                    )
                return InitOutcome(
                    file=None,
                    upload_url="",
                    expires_in=0,
                    replay_body=existing.response_body,
                )

        # Target folder + the owner whose tree/quota this upload lands in.
        # Absent folder_id = the caller's own root (discovery-free top level).
        if folder_id is None:
            root = await self._folders.get_root_for_owner(actor_id)
            if root is None:
                raise NotFoundError("Root folder not found.")
            target_id, owner_id = root.id, root.owner_id
        else:
            folder = await self._folders.get_fresh(folder_id)
            if folder is None:
                raise NotFoundError("Folder not found.")
            target_id, owner_id = folder.id, folder.owner_id

        # Editor+ on the target (placing an entry = a write to its children);
        # a trashed chain resolves non-owners to None (→ 404) and blocks the
        # owner with 422 — identical to the Phase 4 folder-create gate.
        role, chain_trashed = await self._access.resolve_folder_access(
            actor_id, target_id
        )
        if not role_meets(role, AccessRole.EDITOR):
            raise NotFoundError("Folder not found.")
        if chain_trashed:
            raise DomainValidationError(
                "Folder is in the trash (restore it first).", code="trashed"
            )

        # One conditional UPDATE: the race for the last free bytes is decided
        # atomically inside PG. No row updated → over quota → 409.
        if not await self._quota.reserve(owner_id, size):
            raise ConflictError(
                "Storage quota exceeded.",
                code="quota_exceeded",
                details={"owner_id": str(owner_id), "requested": size},
            )

        safe_name = sanitize_filename(filename)
        ext = sanitize_extension(safe_name)
        file_id = uuid.uuid4()
        storage_key = f"{owner_id}/{file_id}" + (f".{ext}" if ext else "")

        file = await self._files.create(
            user_id=owner_id,
            folder_id=target_id,
            original_name=safe_name,
            mime_type=content_type,
            size_bytes=size,
            storage_key=storage_key,
            status=FileStatus.PENDING_UPLOAD,
            upload_expires_at=datetime.now(UTC)
            + timedelta(seconds=settings.UPLOAD_TTL_SECONDS),
        )
        url, expires = await self._storage.presign_put(storage_key, size)

        response_body: dict[str, Any] | None = None
        if idempotency_key:
            body = FileInitOut(file_id=file.id, upload_url=url, expires_in=expires)
            response_body = body.model_dump(mode="json")
            await self._idem.store(
                user_id=actor_id,
                key=idempotency_key,
                request_hash=body_hash,
                response_status=201,
                response_body=response_body,
                expires_at=datetime.now(UTC)
                + timedelta(days=settings.IDEMPOTENCY_TTL_DAYS),
            )

        await self._session.commit()
        logger.info(
            "file_init",
            actor_id=str(actor_id),
            owner_id=str(owner_id),
            file_id=str(file.id),
            size=size,
        )
        return InitOutcome(file=file, upload_url=url, expires_in=expires, replay_body=None)

    # --- complete ---------------------------------------------------------------

    async def complete_upload(
        self, actor_id: uuid.UUID, file_id: uuid.UUID
    ) -> CompleteOutcome:
        file = await self._files.get_fresh(file_id)
        if file is None:
            raise NotFoundError("File not found.")
        role = await self._access.resolve_role(actor_id, file)
        if not role_meets(role, AccessRole.EDITOR):
            raise NotFoundError("File not found.")

        if file.status is not FileStatus.PENDING_UPLOAD:
            # Idempotent no-op: already completed (possibly by a concurrent
            # winner) — 200 with the current state.
            return CompleteOutcome(file=file, transitioned=False)

        meta = await self._storage.head(file.storage_key)
        if meta is None:
            # Client never PUT the bytes (or the presigned URL expired).
            # Reservation KEPT: the URL may still be live for a re-PUT; the
            # Phase 6 sweeper releases abandoned rows at upload_expires_at.
            raise ConflictError(
                "No object was uploaded for this file id.", code="upload_not_found"
            )
        actual_size = int(meta.get("ContentLength", 0))
        declared = file.size_bytes
        if actual_size != declared:
            # Presigned PUT binds Content-Length, so this is tampering or a
            # broken client. Kill the object AND the reservation (spec);
            # zero the row's size so a later delete cannot double-release.
            await self._storage.delete(file.storage_key)
            await self._quota.release(file.user_id, declared)
            file.size_bytes = 0
            await self._session.commit()
            raise ConflictError(
                "Uploaded size differs from the declared size.",
                code="size_mismatch",
                details={"declared": declared, "actual": actual_size},
            )

        head_bytes = await self._storage.read_range(file.storage_key, SNIFF_SAMPLE_BYTES)
        if is_rejected_markup(head_bytes):
            # SVG/HTML never enter the drive (scripting/phishing vectors):
            # same treatment as a size mismatch — object + reservation gone.
            await self._storage.delete(file.storage_key)
            await self._quota.release(file.user_id, file.size_bytes)
            file.size_bytes = 0
            await self._session.commit()
            raise DomainValidationError(
                "This file type (SVG/HTML markup) is not allowed.",
                code="rejected_type",
            )

        sniffed = sniff(file.original_name, head_bytes)
        sha256 = await self._hash_object(file.storage_key)

        # ONE transaction: conditional transition + outbox announcement.
        # WHERE status='pending_upload' makes concurrent completers race to
        # exactly one winner — only the rowcount-1 tx writes the outbox row,
        # so duplicate Celery deliveries are impossible at the source.
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(File)
                .where(File.id == file.id, File.status == FileStatus.PENDING_UPLOAD)
                .values(
                    kind=sniffed.kind,
                    mime_type=sniffed.mime_type,
                    sha256=sha256,
                    size_bytes=actual_size,
                    status=FileStatus.UPLOADED,
                )
            ),
        )
        if result.rowcount == 0:
            # A concurrent completer won the race between our read and now —
            # or the owner deleted the pending row in that window.
            await self._session.rollback()
            fresh = await self._files.get_fresh(file_id)
            if fresh is None:
                raise NotFoundError("File not found.")
            return CompleteOutcome(file=fresh, transitioned=False)

        await self._outbox.insert_event(
            event_type="file.uploaded",
            payload={
                "file_id": str(file.id),
                "owner_id": str(file.user_id),
                "actor_id": str(actor_id),
                "kind": sniffed.kind.value,
                "storage_key": file.storage_key,
                "size_bytes": actual_size,
                "sha256": sha256,
                "request_id": request_id_var.get(),
            },
        )
        await self._session.commit()
        logger.info(
            "file_completed",
            actor_id=str(actor_id),
            owner_id=str(file.user_id),
            file_id=str(file.id),
            kind=sniffed.kind.value,
            sha256=sha256[:12],
        )
        fresh = await self._files.get_fresh(file_id)
        assert fresh is not None  # transitioned by this same committed tx
        return CompleteOutcome(file=fresh, transitioned=True)

    async def _hash_object(self, key: str) -> str:
        """Streaming SHA-256 — constant memory regardless of object size.

        v3 keeps the hash as an upload-integrity check only (declared vs
        computed is compared in the Phase 7 pipeline); there is no
        dedup-by-reuse lookup anymore.
        """
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
        """Own-files listing. Pending uploads are included (user decision:
        clients can poll upload state straight from the listing)."""
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

    async def get_file(self, actor_id: uuid.UUID, file_id: uuid.UUID) -> File:
        file = await self._files.get_fresh(file_id)
        if file is None:
            raise NotFoundError("File not found.")
        # Quarantined files are invisible to reads (the object is gone or
        # going): detail AND presigned views 404, exactly like a missing
        # row. The listing still shows the row (the user must see why their
        # quota dropped / that the file was flagged); rename and delete
        # stay available (delete_file re-checks via get_fresh below).
        if file.status is FileStatus.QUARANTINED:
            raise NotFoundError("File not found.")
        await self._access.require_role(actor_id, file, AccessRole.VIEWER)
        return file

    async def get_view_url(
        self,
        actor_id: uuid.UUID,
        file_id: uuid.UUID,
        download: bool,
        *,
        expires_seconds: int | None = None,
    ) -> tuple[File, str, int]:
        file = await self.get_file(actor_id, file_id)
        # Non-images download as attachments (inline HTML/SVG phishing is
        # exactly what the sniff gate refuses to store); images render.
        force_download = download or file.kind is not FileKind.IMAGE
        url, expires = await self._storage.presign_get(
            file.storage_key,
            force_download,
            filename=sanitize_filename(file.original_name),
            expires=expires_seconds,
        )
        return file, url, expires

    # --- mutate -----------------------------------------------------------------

    async def rename(self, actor_id: uuid.UUID, file_id: uuid.UUID, new_name: str) -> File:
        file = await self._files.get_fresh(file_id)
        if file is None:
            raise NotFoundError("File not found.")
        await self._access.require_role(actor_id, file, AccessRole.OWNER)
        file.original_name = sanitize_filename(new_name)
        await self._session.commit()
        logger.info("file_renamed", actor_id=str(actor_id), file_id=str(file_id))
        return file

    async def delete_file(self, actor_id: uuid.UUID, file_id: uuid.UUID) -> None:
        file = await self._files.get_fresh(file_id)
        if file is None:
            raise NotFoundError("File not found.")
        await self._access.require_role(actor_id, file, AccessRole.OWNER)
        # S3 first: if storage errors, the row survives and the client can
        # retry (S3 delete is idempotent). DB last: a committed row must never
        # point at an object we failed to delete.
        await self._storage.delete(file.storage_key)
        # Release the reservation (completed files charge used bytes; pending
        # rows hold their init reservation). A failed complete already
        # released AND zeroed size_bytes → this is a 0-byte no-op for them.
        # Quarantined rows were released by the quarantine transaction —
        # releasing again here would hand the user free space (guarded
        # release cannot go negative, but accounting would drift).
        if file.status is not FileStatus.QUARANTINED:
            await self._quota.release(file.user_id, file.size_bytes)
        await self._files.delete(file)
        await self._session.commit()
        logger.info(
            "file_deleted", actor_id=str(actor_id), file_id=str(file_id)
        )
