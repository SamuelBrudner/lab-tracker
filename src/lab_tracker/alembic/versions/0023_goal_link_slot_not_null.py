"""Store unslotted goal links as empty strings for uniqueness."""

import sqlalchemy as sa

from alembic import op

revision = "0023_goal_link_slot_not_null"
down_revision = "0022_goals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE goal_links SET slot = '' WHERE slot IS NULL")
    with op.batch_alter_table("goal_links") as batch_op:
        batch_op.alter_column(
            "slot",
            existing_type=sa.String(length=120),
            nullable=False,
            server_default="",
        )


def downgrade() -> None:
    with op.batch_alter_table("goal_links") as batch_op:
        batch_op.alter_column(
            "slot",
            existing_type=sa.String(length=120),
            nullable=True,
            server_default=None,
        )
    op.execute("UPDATE goal_links SET slot = NULL WHERE slot = ''")
