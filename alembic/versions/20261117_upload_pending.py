"""Phase 5: pending_upload state for the verified-at-complete upload flow.

- file_status gains the 'pending_upload' label (PG cannot run ALTER TYPE
  ADD VALUE inside a transaction -> autocommit_block; the value can never be
  REMOVED in a downgrade — PostgreSQL has no DROP VALUE, so downgrade leaves
  the unused label behind, harmless and documented).
- files.upload_expires_at marks an init'd-but-never-completed upload: the
  Phase 6 sweeper deletes such rows and releases their quota reservation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261117_upload_pending"
down_revision: str | None = "20261116_refresh_family_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # Enum ALTERs are transactional-unsafe in PG; IF NOT EXISTS keeps the
        # (dance ×2) re-upgrade idempotent.
        op.execute("ALTER TYPE file_status ADD VALUE IF NOT EXISTS 'pending_upload'")
    op.add_column(
        "files",
        sa.Column("upload_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("files", "upload_expires_at")
    # No DROP VALUE in PostgreSQL — 'pending_upload' stays on the type.
    # No rows can reference it here: this migration's columns are the only
    # place the label is ever written, and they are gone above.
