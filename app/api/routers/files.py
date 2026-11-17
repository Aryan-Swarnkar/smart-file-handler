"""Files API: presigned init/complete upload, browse, view, rename, delete.

Phase 5: Idempotency-Key replay on init (stored 201 body returned verbatim),
complete returns 202, GET /files/{id} embeds a 5-minute presigned GET.
Access filtering happens in FileService via AccessService (denials are 404,
never 403). HTTP shapes only — business rules live in the service layer.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status
from fastapi.responses import JSONResponse

from app.api.deps import CurrentUser, FilesService
from app.core.config import get_settings
from app.core.errors import DomainValidationError
from app.schemas.files import (
    FileDetailOut,
    FileInitIn,
    FileInitOut,
    FileOut,
    FilePageOut,
    FileRenameIn,
    FileUrlOut,
)

router = APIRouter(prefix="/files", tags=["files"])


@router.post(
    "/init",
    response_model=FileInitOut,
    status_code=status.HTTP_201_CREATED,
    summary="Step 1: register the upload and get a presigned PUT url",
)
async def init_upload(
    payload: FileInitIn,
    user: CurrentUser,
    service: FilesService,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> FileInitOut | JSONResponse:
    outcome = await service.init_upload(
        user.id,
        payload.filename,
        payload.content_type,
        payload.size,
        folder_id=payload.folder_id,
        idempotency_key=idempotency_key,
    )
    if outcome.replay_body is not None:
        # Stored response, verbatim — same status (201) and body the client
        # got the first time.
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=outcome.replay_body)
    return FileInitOut(
        file_id=outcome.file.id,  # type: ignore[union-attr]
        upload_url=outcome.upload_url,
        expires_in=outcome.expires_in,
    )


@router.post(
    "/{file_id}/complete",
    response_model=FileOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Step 2: verify + hash + sniff the uploaded bytes",
)
async def complete_upload(
    file_id: uuid.UUID,
    user: CurrentUser,
    service: FilesService,
    response: Response,
) -> FileOut:
    outcome = await service.complete_upload(user.id, file_id)
    # First complete performs the pending→uploaded transition → 202 Accepted
    # (the worker finishes it later). A double/replayed complete of an
    # already-uploaded row is an idempotent no-op → 200 + current state.
    response.status_code = status.HTTP_202_ACCEPTED if outcome.transitioned else 200
    return FileOut.model_validate(outcome.file, from_attributes=True)


@router.get("", response_model=FilePageOut, summary="All Files (keyset pagination)")
async def list_files(
    user: CurrentUser,
    service: FilesService,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
    doc_topic: Annotated[str | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> FilePageOut:
    from app.db.models import FileKind, FileStatus

    try:
        kind_value = FileKind(kind) if kind else None
        status_value = FileStatus(status_filter) if status_filter else None
    except ValueError as exc:
        # Invalid enum query params → 422, never a 500.
        raise DomainValidationError(
            "Invalid filter value; kind must be one of image|pdf|other, "
            "status one of pending_upload|uploaded|processing|done|failed.",
            code="bad_filter",
        ) from exc
    page = await service.list_files(
        user.id,
        limit=limit,
        cursor=cursor,
        kind=kind_value,
        doc_topic=doc_topic,
        status=status_value,
    )
    return FilePageOut(
        items=[FileOut.model_validate(f, from_attributes=True) for f in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{file_id}", response_model=FileDetailOut, summary="One file's metadata + view url")
async def get_file(
    file_id: uuid.UUID, user: CurrentUser, service: FilesService
) -> FileDetailOut:
    file, url, expires = await service.get_view_url(
        user.id,
        file_id,
        download=False,
        # The spec's 5-minute embed TTL; /url keeps the phase-2 knob.
        expires_seconds=get_settings().FILE_URL_EXPIRE_SECONDS,
    )
    detail = FileDetailOut(
        **FileOut.model_validate(file, from_attributes=True).model_dump(),
        url=url,
        url_expires_in=expires,
    )
    return detail


@router.get(
    "/{file_id}/url", response_model=FileUrlOut, summary="Presigned view/download url"
)
async def get_file_url(
    file_id: uuid.UUID,
    user: CurrentUser,
    service: FilesService,
    download: Annotated[bool, Query()] = False,
) -> FileUrlOut:
    _, url, expires = await service.get_view_url(user.id, file_id, download)
    return FileUrlOut(url=url, expires_in=expires)


@router.patch("/{file_id}", response_model=FileOut, summary="Rename a file")
async def rename_file(
    file_id: uuid.UUID,
    payload: FileRenameIn,
    user: CurrentUser,
    service: FilesService,
) -> FileOut:
    file = await service.rename(user.id, file_id, payload.original_name)
    return FileOut.model_validate(file, from_attributes=True)


@router.delete(
    "/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete file (hard: row + stored object + quota release)",
)
async def delete_file(
    file_id: uuid.UUID, user: CurrentUser, service: FilesService
) -> Response:
    await service.delete_file(user.id, file_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
