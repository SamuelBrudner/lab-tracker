"""Add graph draft batch settings and run history."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0024_graph_draft_batches"
down_revision = "0023_goal_link_slot_not_null"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("graph_change_sets", sa.Column("source_note_ids", sa.JSON(), nullable=True))
    op.add_column("graph_change_sets", sa.Column("batch_key", sa.String(length=120), nullable=True))
    op.add_column(
        "graph_change_sets",
        sa.Column("batch_window_start", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "graph_change_sets",
        sa.Column("batch_window_end", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_graph_change_sets_batch_key",
        "graph_change_sets",
        ["batch_key"],
        unique=True,
    )
    op.add_column("graph_change_operations", sa.Column("review_note", sa.Text(), nullable=True))

    op.create_table(
        "graph_draft_batch_settings",
        sa.Column("settings_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("cadence_minutes", sa.Integer(), nullable=False, server_default="1440"),
        sa.Column("run_at_local_time", sa.String(length=5), nullable=False, server_default="06:00"),
        sa.Column(
            "timezone_name",
            sa.String(length=100),
            nullable=False,
            server_default="America/New_York",
        ),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("settings_id"),
        sa.UniqueConstraint("project_id", name="uq_graph_draft_batch_settings_project"),
    )
    op.create_index(
        "ix_graph_draft_batch_settings_next_run_at",
        "graph_draft_batch_settings",
        ["next_run_at"],
        unique=False,
    )

    op.create_table(
        "graph_draft_batch_runs",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("batch_key", sa.String(length=120), nullable=False),
        sa.Column("change_set_id", sa.String(length=36), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("error_metadata", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["graph_change_sets.change_set_id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("batch_key", name="uq_graph_draft_batch_runs_batch_key"),
    )
    op.create_index(
        "ix_graph_draft_batch_runs_project_window_end",
        "graph_draft_batch_runs",
        ["project_id", "window_end"],
        unique=False,
    )
    op.create_index(
        "ix_graph_draft_batch_runs_status_created_at",
        "graph_draft_batch_runs",
        ["status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_graph_draft_batch_runs_status_created_at",
        table_name="graph_draft_batch_runs",
    )
    op.drop_index(
        "ix_graph_draft_batch_runs_project_window_end",
        table_name="graph_draft_batch_runs",
    )
    op.drop_table("graph_draft_batch_runs")
    op.drop_index(
        "ix_graph_draft_batch_settings_next_run_at",
        table_name="graph_draft_batch_settings",
    )
    op.drop_table("graph_draft_batch_settings")
    op.drop_column("graph_change_operations", "review_note")
    op.drop_index("ix_graph_change_sets_batch_key", table_name="graph_change_sets")
    op.drop_column("graph_change_sets", "batch_window_end")
    op.drop_column("graph_change_sets", "batch_window_start")
    op.drop_column("graph_change_sets", "batch_key")
    op.drop_column("graph_change_sets", "source_note_ids")
