"""Track entity origin and graph draft backlinks."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035_entity_origin_backlinks"
down_revision = "0034_terminal_transition_reasons"
branch_labels = None
depends_on = None

_ORIGIN_TABLES = (
    "questions",
    "datasets",
    "notes",
    "sessions",
    "analyses",
    "claims",
    "goals",
    "visualizations",
)


def _add_origin_columns(table_name: str) -> None:
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.add_column(
            sa.Column("origin", sa.String(length=32), nullable=False, server_default="user")
        )
        batch_op.add_column(
            sa.Column("change_set_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("origin_provider", sa.String(length=80), nullable=True)
        )
        batch_op.add_column(
            sa.Column("origin_model", sa.String(length=255), nullable=True)
        )
        batch_op.add_column(
            sa.Column("origin_prompt_version", sa.String(length=120), nullable=True)
        )
        batch_op.create_foreign_key(
            f"fk_{table_name}_change_set_id_graph_change_sets",
            "graph_change_sets",
            ["change_set_id"],
            ["change_set_id"],
            ondelete="SET NULL",
        )
    op.create_index(f"ix_{table_name}_change_set_id", table_name, ["change_set_id"])


def _drop_origin_columns(table_name: str) -> None:
    op.drop_index(f"ix_{table_name}_change_set_id", table_name=table_name)
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.drop_constraint(
            f"fk_{table_name}_change_set_id_graph_change_sets",
            type_="foreignkey",
        )
        batch_op.drop_column("origin_prompt_version")
        batch_op.drop_column("origin_model")
        batch_op.drop_column("origin_provider")
        batch_op.drop_column("change_set_id")
        batch_op.drop_column("origin")


def upgrade() -> None:
    op.add_column("claims", sa.Column("created_by", sa.String(length=255), nullable=True))
    with op.batch_alter_table("claims") as batch_op:
        batch_op.add_column(
            sa.Column("created_by_user_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_claims_created_by_user_id_users",
            "users",
            ["created_by_user_id"],
            ["user_id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_claims_created_by_user_id", "claims", ["created_by_user_id"])

    op.add_column(
        "visualizations",
        sa.Column("created_by", sa.String(length=255), nullable=True),
    )
    with op.batch_alter_table("visualizations") as batch_op:
        batch_op.add_column(
            sa.Column("created_by_user_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_visualizations_created_by_user_id_users",
            "users",
            ["created_by_user_id"],
            ["user_id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_visualizations_created_by_user_id",
        "visualizations",
        ["created_by_user_id"],
    )

    for table_name in _ORIGIN_TABLES:
        _add_origin_columns(table_name)


def downgrade() -> None:
    for table_name in reversed(_ORIGIN_TABLES):
        _drop_origin_columns(table_name)

    op.drop_index("ix_visualizations_created_by_user_id", table_name="visualizations")
    with op.batch_alter_table("visualizations") as batch_op:
        batch_op.drop_constraint(
            "fk_visualizations_created_by_user_id_users",
            type_="foreignkey",
        )
        batch_op.drop_column("created_by_user_id")
    op.drop_column("visualizations", "created_by")

    op.drop_index("ix_claims_created_by_user_id", table_name="claims")
    with op.batch_alter_table("claims") as batch_op:
        batch_op.drop_constraint(
            "fk_claims_created_by_user_id_users",
            type_="foreignkey",
        )
        batch_op.drop_column("created_by_user_id")
    op.drop_column("claims", "created_by")
