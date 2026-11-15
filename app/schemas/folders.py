"""Request/response models for the folders API (Phase 4)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SortParam = Literal["name", "created_at"]


class FolderCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # REQUIRED: every user already owns exactly one root ("My Files") — the
    # client discovers its id via GET /folders/root; a second root would
    # break the single-root invariant.
    parent_id: UUID


class FolderMoveIn(BaseModel):
    parent_id: UUID


class FolderRenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class FolderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    parent_id: UUID | None
    owner_id: UUID
    deleted_at: datetime | None
    created_at: datetime


class FolderPageOut(BaseModel):
    items: list[FolderOut]
    next_cursor: str | None
