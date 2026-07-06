"""Add server-resident graph drafting queue fields.

Revision ID: 0051_server_resident_agentic_drafting
Revises: 0050_data_store_group_scope
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0051_server_resident_agentic_drafting"
down_revision = "0050_data_store_group_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _set_sqlite_foreign_keys(enabled=False)
    try:
        with op.batch_alter_table("graph_change_sets") as batch_op:
            batch_op.add_column(sa.Column("review_assignee", sa.String(length=255)))
            batch_op.add_column(sa.Column("review_assignee_user_id", sa.String(length=36)))
            batch_op.create_foreign_key(
                "fk_graph_change_sets_review_assignee_user_id_users",
                "users",
                ["review_assignee_user_id"],
                ["user_id"],
                ondelete="SET NULL",
            )
        op.create_index(
            "ix_graph_change_sets_review_assignee_user_id",
            "graph_change_sets",
            ["review_assignee_user_id"],
        )

        with op.batch_alter_table("graph_draft_batch_settings") as batch_op:
            batch_op.drop_constraint(
                "uq_graph_draft_batch_settings_project",
                type_="unique",
            )
            batch_op.add_column(sa.Column("user_id", sa.String(length=36)))
            batch_op.create_foreign_key(
                "fk_graph_draft_batch_settings_user_id_users",
                "users",
                ["user_id"],
                ["user_id"],
                ondelete="CASCADE",
            )
            batch_op.create_unique_constraint(
                "uq_graph_draft_batch_settings_project_user",
                ["project_id", "user_id"],
            )
        op.create_index(
            "ix_graph_draft_batch_settings_project_user",
            "graph_draft_batch_settings",
            ["project_id", "user_id"],
        )
        op.create_index(
            "uq_graph_draft_batch_settings_project_default",
            "graph_draft_batch_settings",
            ["project_id"],
            unique=True,
            sqlite_where=sa.text("user_id IS NULL"),
            postgresql_where=sa.text("user_id IS NULL"),
        )

        with op.batch_alter_table("graph_draft_batch_runs") as batch_op:
            batch_op.add_column(sa.Column("source_note_ids", sa.JSON()))
            batch_op.add_column(sa.Column("user_hint", sa.Text()))
            batch_op.add_column(sa.Column("review_assignee", sa.String(length=255)))
            batch_op.add_column(sa.Column("review_assignee_user_id", sa.String(length=36)))
            batch_op.create_foreign_key(
                "fk_graph_draft_batch_runs_review_assignee_user_id_users",
                "users",
                ["review_assignee_user_id"],
                ["user_id"],
                ondelete="SET NULL",
            )
        op.create_index(
            "ix_graph_draft_batch_runs_review_assignee_user_id",
            "graph_draft_batch_runs",
            ["review_assignee_user_id"],
        )
    finally:
        _set_sqlite_foreign_keys(enabled=True)


def downgrade() -> None:
    _set_sqlite_foreign_keys(enabled=False)
    try:
        op.drop_index(
            "ix_graph_draft_batch_runs_review_assignee_user_id",
            table_name="graph_draft_batch_runs",
        )
        with op.batch_alter_table("graph_draft_batch_runs") as batch_op:
            batch_op.drop_constraint(
                "fk_graph_draft_batch_runs_review_assignee_user_id_users",
                type_="foreignkey",
            )
            batch_op.drop_column("review_assignee_user_id")
            batch_op.drop_column("review_assignee")
            batch_op.drop_column("user_hint")
            batch_op.drop_column("source_note_ids")

        op.drop_index(
            "uq_graph_draft_batch_settings_project_default",
            table_name="graph_draft_batch_settings",
        )
        op.drop_index(
            "ix_graph_draft_batch_settings_project_user",
            table_name="graph_draft_batch_settings",
        )
        with op.batch_alter_table("graph_draft_batch_settings") as batch_op:
            batch_op.drop_constraint(
                "uq_graph_draft_batch_settings_project_user",
                type_="unique",
            )
            batch_op.drop_constraint(
                "fk_graph_draft_batch_settings_user_id_users",
                type_="foreignkey",
            )
            batch_op.drop_column("user_id")
            batch_op.create_unique_constraint(
                "uq_graph_draft_batch_settings_project",
                ["project_id"],
            )

        op.drop_index(
            "ix_graph_change_sets_review_assignee_user_id",
            table_name="graph_change_sets",
        )
        with op.batch_alter_table("graph_change_sets") as batch_op:
            batch_op.drop_constraint(
                "fk_graph_change_sets_review_assignee_user_id_users",
                type_="foreignkey",
            )
            batch_op.drop_column("review_assignee_user_id")
            batch_op.drop_column("review_assignee")
    finally:
        _set_sqlite_foreign_keys(enabled=True)


def _set_sqlite_foreign_keys(*, enabled: bool) -> None:
    if op.get_context().dialect.name == "sqlite":
        value = "ON" if enabled else "OFF"
        op.execute(f"PRAGMA foreign_keys={value}")
