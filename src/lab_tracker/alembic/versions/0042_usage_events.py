"""Add local usage telemetry tables."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0042_usage_events"
down_revision = "0041_dedupe_unslotted_goal_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verb", sa.String(length=30), nullable=False),
        sa.Column("resource_type", sa.String(length=40), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=True),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("actor_role", sa.String(length=30), nullable=True),
        sa.Column("principal_type", sa.String(length=20), nullable=True),
        sa.Column("surface", sa.String(length=20), nullable=True),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("result_count", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_usage_events_occurred_at", "usage_events", ["occurred_at"])
    op.create_index("ix_usage_events_verb", "usage_events", ["verb"])
    op.create_index("ix_usage_events_resource_type", "usage_events", ["resource_type"])
    op.create_index("ix_usage_events_project_id", "usage_events", ["project_id"])
    op.create_index("ix_usage_events_actor_user_id", "usage_events", ["actor_user_id"])

    op.create_table(
        "usage_event_rollups",
        sa.Column("rollup_id", sa.String(length=36), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("verb", sa.String(length=30), nullable=False),
        sa.Column("resource_type", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("actor_role", sa.String(length=30), nullable=True),
        sa.Column("principal_type", sa.String(length=20), nullable=True),
        sa.Column("surface", sa.String(length=20), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("total_duration_ms", sa.Integer(), nullable=False),
        sa.Column("total_result_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("rollup_id"),
        sa.UniqueConstraint(
            "day",
            "verb",
            "resource_type",
            "project_id",
            "actor_role",
            "principal_type",
            "surface",
            "outcome",
            name="uq_usage_event_rollup_bucket",
        ),
    )
    op.create_index("ix_usage_event_rollups_day", "usage_event_rollups", ["day"])
    op.create_index(
        "ix_usage_event_rollups_bucket",
        "usage_event_rollups",
        ["verb", "resource_type", "day"],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_event_rollups_bucket", table_name="usage_event_rollups")
    op.drop_index("ix_usage_event_rollups_day", table_name="usage_event_rollups")
    op.drop_table("usage_event_rollups")
    op.drop_index("ix_usage_events_actor_user_id", table_name="usage_events")
    op.drop_index("ix_usage_events_project_id", table_name="usage_events")
    op.drop_index("ix_usage_events_resource_type", table_name="usage_events")
    op.drop_index("ix_usage_events_verb", table_name="usage_events")
    op.drop_index("ix_usage_events_occurred_at", table_name="usage_events")
    op.drop_table("usage_events")
