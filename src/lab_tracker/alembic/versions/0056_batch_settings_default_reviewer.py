"""Add the project default reviewer to graph draft batch settings.

Staged notes with no routable reviewer (unattributed captures, or captures
whose derived reviewer lost access) fall back to this reviewer instead of
being silently dropped from scheduled batches (bead lab-tracker-ul0n.1).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0056_batch_settings_default_reviewer"
down_revision = "0055_graph_change_set_note_dispositions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("graph_draft_batch_settings") as batch_op:
        batch_op.add_column(
            sa.Column("default_reviewer_user_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_graph_draft_batch_settings_default_reviewer_user_id_users",
            "users",
            ["default_reviewer_user_id"],
            ["user_id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("graph_draft_batch_settings") as batch_op:
        batch_op.drop_constraint(
            "fk_graph_draft_batch_settings_default_reviewer_user_id_users",
            type_="foreignkey",
        )
        batch_op.drop_column("default_reviewer_user_id")
