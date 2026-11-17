"""Request/response models for the files API (Phase 5 shapes).

FileDetailOut extends FileOut with the 5-minute presigned GET url — additive
only, the frozen web UI keeps its existing shapes (FileOut/FilePageOut/...).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# v3 allowlist (Phase 5 sniff): jpeg/png/webp/gif -> image, pdf -> pdf,
# everything else -> other. document/text were v2 kinds — gone.
FileKindLiteral = Literal["image", "pdf", "other"]
FileStatusLiteral = Literal[
    "pending_upload", "uploaded", "processing", "done", "failed", "quarantined"
]


class FileInitIn(BaseModel):
    filename: str = Field(min_length=1, max_length=1024)
    # Advisory only — /complete re-derives the real mime from magic bytes.
    content_type: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)  # ≤ MAX_UPLOAD_MB enforced in the service (env-driven)
    # Absent = the caller's own root ("My Files"); present = editor+ on that
    # folder, and the upload is charged to the folder OWNER's quota.
    folder_id: UUID | None = None


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
    # Phase 6: worker-written terminal reason ("processing failed after N
    # attempts: ...", "format: ...") — NULL while the file is alive. Additive
    # response field (frozen web UI keeps working; extra keys are ignored).
    error: str | None
    created_at: datetime


class FileDetailOut(FileOut):
    """GET /files/{id}: metadata + a 5-minute presigned GET url. Images
    render inline; every other kind (and explicit ?download=true) is signed
    with an attachment disposition carrying the sanitized filename."""

    url: str
    url_expires_in: int


class FileRenameIn(BaseModel):
    original_name: str = Field(min_length=1, max_length=255)


class FileUrlOut(BaseModel):
    url: str
    expires_in: int


class FilePageOut(BaseModel):
    items: list[FileOut]
    next_cursor: str | None
