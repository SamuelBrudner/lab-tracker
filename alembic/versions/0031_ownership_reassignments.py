"""Add ownership reassignment audit records."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0031_ownership_reassignments"
down_revision = "0030_nullable_goal_project"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ownership_reassignments",
        sa.Column("reassignment_id", sa.String(length=36), nullable=False),
        sa.Column("from_user_id", sa.String(length=36), nullable=False),
        sa.Column("to_user_id", sa.String(length=36), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("record_counts", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["from_user_id"],
            ["users.user_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_user_id"],
            ["users.user_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.user_id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("reassignment_id"),
    )
    op.create_index(
        "ix_ownership_reassignments_from_created",
        "ownership_reassignments",
        ["from_user_id", "created_at"],
    )
    op.create_index(
        "ix_ownership_reassignments_to_created",
        "ownership_reassignments",
        ["to_user_id", "created_at"],
    )
    op.create_index(
        "ix_ownership_reassignments_created_by_user_id",
        "ownership_reassignments",
        ["created_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ownership_reassignments_created_by_user_id",
        table_name="ownership_reassignments",
    )
    op.drop_index(
        "ix_ownership_reassignments_to_created",
        table_name="ownership_reassignments",
    )
    op.drop_index(
        "ix_ownership_reassignments_from_created",
        table_name="ownership_reassignments",
    )
    op.drop_table("ownership_reassignments")
