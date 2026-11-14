"""permissions — direct viewer|editor grants on one file OR one folder.

The XOR is enforced by the DB (ck_permissions_exactly_one_target): a grant
row targets exactly one thing. Folder grants are what folder descendants
inherit (AccessService resolves the effective role, Phase 4).
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin


class PermissionRole(enum.StrEnum):
    VIEWER = "viewer"
    EDITOR = "editor"


permission_role_enum = SQLAlchemyEnum(
    PermissionRole,
    name="permission_role",
    native_enum=True,
    values_callable=lambda e: [m.value for m in e],
)


class Permission(Base, CreatedAtMixin):
    __tablename__ = "permissions"
    __table_args__ = (
        CheckConstraint(
            "(file_id IS NULL) <> (folder_id IS NULL)",
            name="ck_permissions_exactly_one_target",
        ),
        # One grant per (grantee, target). Two separate constraints because
        # each target column is NULL exactly when the other isn't; PG unique
        # treats NULLs as distinct, so folder grants never trip the file one.
        UniqueConstraint(
            "grantee_id", "file_id", name="uq_permissions_grantee_file"
        ),
        UniqueConstraint(
            "grantee_id", "folder_id", name="uq_permissions_grantee_folder"
        ),
        # Phase 8 "who is this shared with" listing (reverse lookup).
        Index("ix_permissions_file", "file_id"),
        Index("ix_permissions_folder", "folder_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    grantee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    granted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        # Informational; the grant dies with its target anyway (file/folder
        # CASCADE), so losing the granter's id on account deletion is fine.
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    role: Mapped[PermissionRole] = mapped_column(
        permission_role_enum, nullable=False
    )
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"),
        nullable=True,
    )
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"),
        nullable=True,
    )
