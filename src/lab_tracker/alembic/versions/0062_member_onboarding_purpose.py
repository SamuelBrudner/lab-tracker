"""Add graph-draft purpose for constrained member onboarding proposals."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0062_member_onboarding_purpose"
down_revision = "0061_graph_draft_generation_fencing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("graph_change_sets") as batch_op:
        batch_op.add_column(
            sa.Column(
                "purpose",
                sa.String(length=48),
                nullable=False,
                server_default="general",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("graph_change_sets") as batch_op:
        batch_op.drop_column("purpose")
