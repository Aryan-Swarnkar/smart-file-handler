"""refresh token families + root-folder backfill.

Phase 3 (spec PHASE 3): refresh tokens gain family_id — one login session is
one family; rotation stays inside the family; presenting an already-rotated
token revokes the whole family (not all of the user's sessions, which was the
v2 behavior now superseded).

Also backfills the per-user ROOT folder ("My Files", parent_id NULL) that
Phase 3 registration now creates in the same transaction as the account —
accounts that predate this migration must not browse a rootless drive.

Revision ID: 20261116_refresh_family_id
Revises: 20261115_v3_initial
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.models.folder import ROOT_FOLDER_NAME

revision = "20261116_refresh_family_id"
down_revision = "20261115_v3_initial"
branch_labels = None
depends_on = None

UUID = postgresql.UUID

# Single backfill statement, shared by upgrade so the column is never NULL in
# a committed state (add nullable -> backfill -> NOT NULL, the standard
# zero-downtime pattern).
_BACKFILL_FAMILY = """
UPDATE refresh_tokens SET family_id = id WHERE family_id IS NULL
"""

# One root folder per user missing one. parent_id IS NULL is the root marker;
# the NOT EXISTS guard makes the statement idempotent (re-runs insert nothing).
_BACKFILL_ROOT_FOLDERS = """
INSERT INTO folders (id, owner_id, parent_id, name, created_at)
SELECT gen_random_uuid(), u.id, NULL, :root_name, now()
FROM users u
WHERE NOT EXISTS (
    SELECT 1 FROM folders f
    WHERE f.owner_id = u.id AND f.parent_id IS NULL
)
"""


def upgrade() -> None:
    # --- refresh_tokens.family_id -------------------------------------------
    op.add_column("refresh_tokens", sa.Column("family_id", UUID(), nullable=True))
    op.execute(sa.text(_BACKFILL_FAMILY))
    op.alter_column("refresh_tokens", "family_id", nullable=False)

    # Composite index for the revoke-the-family UPDATE; its leftmost prefix
    # (user_id) covers every query the dropped single-column index served.
    op.create_index(
        "ix_refresh_tokens_user_family",
        "refresh_tokens",
        ["user_id", "family_id"],
    )
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")

    # --- root folder backfill (Phase 3 registration semantics, retroactively) ---
    op.execute(sa.text(_BACKFILL_ROOT_FOLDERS).bindparams(root_name=ROOT_FOLDER_NAME))


def downgrade() -> None:
    # --- undo the family column ----------------------------------------------
    op.drop_index("ix_refresh_tokens_user_family", table_name="refresh_tokens")
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.drop_column("refresh_tokens", "family_id")

    # Remove ONLY the empty root folders this upgrade backfilled: a folder
    # named like the root with parent_id NULL that has no child folders and no
    # files. Users who already stored something in their root keep it (a
    # downgrade must not destroy data this migration did not create).
    op.execute(
        sa.text(
            """
            DELETE FROM folders f
            WHERE f.name = :root_name AND f.parent_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM folders c WHERE c.parent_id = f.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM files fl WHERE fl.folder_id = f.id
              )
            """
        ).bindparams(root_name=ROOT_FOLDER_NAME)
    )
