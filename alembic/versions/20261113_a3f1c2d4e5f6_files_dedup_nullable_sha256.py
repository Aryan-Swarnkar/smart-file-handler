"""files dedup: nullable sha256 + unique (user_id, sha256)

Revision ID: a3f1c2d4e5f6
Revises: bbecbe6aa8ec
Create Date: 2026-11-13 10:00:00.000000

Phase 3: sha256 becomes NULL between init (presigned URL issued) and complete
(client bytes hashed server-side); the unique constraint turns the per-user
byte-level dedup check into an index-backed lookup. NULLs are distinct under
PostgreSQL's default NULLS DISTINCT, so multiple pending uploads never collide.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f1c2d4e5f6"
down_revision: str | None = "bbecbe6aa8ec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "files", "sha256", existing_type=sa.String(length=64), nullable=True
    )
    op.create_unique_constraint(
        "uq_files_user_id_sha256", "files", ["user_id", "sha256"]
    )


def downgrade() -> None:
    # Backfill NULLs before re-adding NOT NULL (constraint would fail on
    # pending rows). Dev data only; production runs this pre-worker anyway.
    op.execute("UPDATE files SET sha256 = 'pending-' || id::text WHERE sha256 IS NULL")
    op.drop_constraint("uq_files_user_id_sha256", "files", type_="unique")
    op.alter_column(
        "files", "sha256", existing_type=sa.String(length=64), nullable=False
    )
