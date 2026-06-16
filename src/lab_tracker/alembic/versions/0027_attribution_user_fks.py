"""Add attribution user foreign-key mirrors."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0027_attribution_user_fks"
down_revision = "0026_project_groups"
branch_labels = None
depends_on = None

CREATED_BY_TABLES = (
    "project_groups",
    "projects",
    "questions",
    "question_refactors",
    "datasets",
    "notes",
    "graph_change_sets",
    "graph_draft_batch_runs",
    "sessions",
    "goals",
    "goal_links",
    "project_memberships",
    "group_memberships",
)


def upgrade() -> None:
    for table_name in CREATED_BY_TABLES:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(
                sa.Column("created_by_user_id", sa.String(length=36), nullable=True)
            )
            batch_op.create_foreign_key(
                f"fk_{table_name}_created_by_user_id_users",
                "users",
                ["created_by_user_id"],
                ["user_id"],
                ondelete="SET NULL",
            )
        op.create_index(
            f"ix_{table_name}_created_by_user_id",
            table_name,
            ["created_by_user_id"],
        )

    with op.batch_alter_table("analyses") as batch_op:
        batch_op.add_column(
            sa.Column("executed_by_user_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_analyses_executed_by_user_id_users",
            "users",
            ["executed_by_user_id"],
            ["user_id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_analyses_executed_by_user_id",
        "analyses",
        ["executed_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_analyses_executed_by_user_id", table_name="analyses")
    with op.batch_alter_table("analyses") as batch_op:
        batch_op.drop_constraint(
            "fk_analyses_executed_by_user_id_users",
            type_="foreignkey",
        )
        batch_op.drop_column("executed_by_user_id")

    for table_name in reversed(CREATED_BY_TABLES):
        op.drop_index(f"ix_{table_name}_created_by_user_id", table_name=table_name)
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(
                f"fk_{table_name}_created_by_user_id_users",
                type_="foreignkey",
            )
            batch_op.drop_column("created_by_user_id")
