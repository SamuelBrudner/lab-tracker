"""Add record export audit events."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0032_record_export_events"
down_revision = "0031_ownership_reassignments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "record_export_events",
        sa.Column("export_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("group_id", sa.String(length=36), nullable=True),
        sa.Column("project_ids", sa.JSON(), nullable=False),
        sa.Column("record_counts", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["project_groups.group_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.user_id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("export_id"),
    )
    op.create_index(
        "ix_record_export_events_user_created",
        "record_export_events",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_record_export_events_group_created",
        "record_export_events",
        ["group_id", "created_at"],
    )
    op.create_index(
        "ix_record_export_events_created_by_user_id",
        "record_export_events",
        ["created_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_record_export_events_created_by_user_id",
        table_name="record_export_events",
    )
    op.drop_index("ix_record_export_events_group_created", table_name="record_export_events")
    op.drop_index("ix_record_export_events_user_created", table_name="record_export_events")
    op.drop_table("record_export_events")
