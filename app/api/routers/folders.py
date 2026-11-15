"""Folders API (Phase 4): root discovery, create, read, children, move,
trash, restore.

Every route is Bearer-protected; access filtering happens in FolderService
via AccessService (no access → 404, never 403). HTTP shapes only — business
rules live in the service layer.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import CurrentUser, FoldersService
from app.core.errors import DomainValidationError
from app.schemas.folders import (
    FolderCreateIn,
    FolderMoveIn,
    FolderOut,
    FolderPageOut,
    FolderRenameIn,
)

router = APIRouter(prefix="/folders", tags=["folders"])


@router.get(
    "/root",
    response_model=FolderOut,
    summary="The caller's root folder (parent_id for every new folder)",
)
async def get_root_folder(user: CurrentUser, service: FoldersService) -> FolderOut:
    folder = await service.get_root(user.id)
    return FolderOut.model_validate(folder, from_attributes=True)


@router.post(
    "",
    response_model=FolderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a folder inside an editable parent",
)
async def create_folder(
    payload: FolderCreateIn, user: CurrentUser, service: FoldersService
) -> FolderOut:
    folder = await service.create(user.id, payload.name, payload.parent_id)
    return FolderOut.model_validate(folder, from_attributes=True)


@router.get(
    "/{folder_id}",
    response_model=FolderOut,
    summary="One folder's metadata",
)
async def get_folder(
    folder_id: uuid.UUID, user: CurrentUser, service: FoldersService
) -> FolderOut:
    folder = await service.get(user.id, folder_id)
    return FolderOut.model_validate(folder, from_attributes=True)


@router.patch(
    "/{folder_id}",
    response_model=FolderOut,
    summary="Rename a folder",
)
async def rename_folder(
    folder_id: uuid.UUID,
    payload: FolderRenameIn,
    user: CurrentUser,
    service: FoldersService,
) -> FolderOut:
    folder = await service.rename(user.id, folder_id, payload.name)
    return FolderOut.model_validate(folder, from_attributes=True)


@router.get(
    "/{folder_id}/children",
    response_model=FolderPageOut,
    summary="Live subfolders (keyset pagination, name or created_at sort)",
)
async def list_children(
    folder_id: uuid.UUID,
    user: CurrentUser,
    service: FoldersService,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "name",
) -> FolderPageOut:
    if sort not in ("name", "created_at"):
        # Invalid enum query params → 422, never a 500 (files router precedent).
        raise DomainValidationError(
            "Invalid sort value; must be one of name|created_at.",
            code="bad_filter",
        )
    page = await service.list_children(
        user.id,
        folder_id,
        limit=limit,
        cursor=cursor,
        sort=sort,  # type: ignore[arg-type]  # validated just above
    )
    return FolderPageOut(
        items=[FolderOut.model_validate(f, from_attributes=True) for f in page.items],
        next_cursor=page.next_cursor,
    )


@router.post(
    "/{folder_id}/move",
    response_model=FolderOut,
    summary="Reparent the folder (cycle- and depth-checked)",
)
async def move_folder(
    folder_id: uuid.UUID,
    payload: FolderMoveIn,
    user: CurrentUser,
    service: FoldersService,
) -> FolderOut:
    folder = await service.move(user.id, folder_id, payload.parent_id)
    return FolderOut.model_validate(folder, from_attributes=True)


@router.post(
    "/{folder_id}/trash",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Move the folder to the trash (idempotent)",
)
async def trash_folder(
    folder_id: uuid.UUID, user: CurrentUser, service: FoldersService
) -> Response:
    await service.trash(user.id, folder_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{folder_id}/restore",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Restore the folder from the trash (idempotent)",
)
async def restore_folder(
    folder_id: uuid.UUID, user: CurrentUser, service: FoldersService
) -> Response:
    await service.restore(user.id, folder_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
