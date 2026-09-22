"""Add a per-user session epoch so session JWTs can be revoked."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0063_user_session_epoch"
down_revision = "0062_member_onboarding_purpose"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column(
                "session_epoch",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    # SQLite drops the column by rebuilding ``users`` (copy, DROP, rename).
    # env.py runs every SQLite migration with foreign keys suspended and checks
    # referential integrity before commit, so the rebuild cannot cascade into
    # tables that reference users.
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("session_epoch")
