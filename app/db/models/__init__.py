"""All ORM models. Importing this module registers every table on Base.metadata
(Alembic target_metadata depends on that side effect)."""

from app.db.models.audit_log import AuditLog
from app.db.models.file import File, FileKind, FileStatus
from app.db.models.folder import Folder
from app.db.models.idempotency_key import IdempotencyKey
from app.db.models.label import FileLabel, Label, LabelKind
from app.db.models.outbox import Outbox
from app.db.models.permission import Permission, PermissionRole
from app.db.models.refresh_token import RefreshToken
from app.db.models.share_link import ShareLink
from app.db.models.user import User

__all__ = [
    "AuditLog",
    "File",
    "FileKind",
    "FileLabel",
    "FileStatus",
    "Folder",
    "IdempotencyKey",
    "Label",
    "LabelKind",
    "Outbox",
    "Permission",
    "PermissionRole",
    "RefreshToken",
    "ShareLink",
    "User",
]
