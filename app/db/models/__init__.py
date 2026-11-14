"""All ORM models. Importing this module registers every table on Base.metadata
(Alembic target_metadata depends on that side effect)."""

from app.db.models.file import File, FileKind, FileStatus
from app.db.models.label import FileLabel, Label, LabelKind
from app.db.models.refresh_token import RefreshToken
from app.db.models.user import User

__all__ = [
    "File",
    "FileKind",
    "FileLabel",
    "FileStatus",
    "Label",
    "LabelKind",
    "RefreshToken",
    "User",
]
