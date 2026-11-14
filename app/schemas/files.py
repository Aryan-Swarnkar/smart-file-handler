"""Request/response models for the files API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

FileKindLiteral = Literal["image", "pdf", "document", "text", "other"]
FileStatusLiteral = Literal["uploaded", "processing", "done", "failed"]


class FileInitIn(BaseModel):
    filename: str = Field(min_length=1, max_length=1024)
    # Advisory only — /complete re-derives the real mime from magic bytes.
    content_type: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)  # ≤ MAX_UPLOAD_MB enforced in the service (env-driven)


class FileInitOut(BaseModel):
    file_id: UUID
    upload_url: str
    expires_in: int


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    original_name: str
    mime_type: str
    size_bytes: int
    sha256: str | None
    status: FileStatusLiteral
    kind: FileKindLiteral | None
    doc_topic: str | None
    created_at: datetime


class FileRenameIn(BaseModel):
    original_name: str = Field(min_length=1, max_length=255)


class FileUrlOut(BaseModel):
    url: str
    expires_in: int


class FilePageOut(BaseModel):
    items: list[FileOut]
    next_cursor: str | None
