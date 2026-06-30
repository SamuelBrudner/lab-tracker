"""Allow group-scoped data stores.

A data store is scoped to exactly one of a project or a group (lab). This makes
``project_id`` nullable and adds a nullable ``group_id`` so a lab can register a
store once and have every project in the group inherit it.

Revision ID: 0050_data_store_group_scope
Revises: 0049_data_stores
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050_data_store_group_scope"
down_revision = "0049_data_stores"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add the FK as a *named* constraint (not inline on the column) so SQLite
    # batch recreate can reproduce it; an unnamed constraint fails the rebuild.
    with op.batch_alter_table("data_stores") as batch_op:
        batch_op.alter_column(
            "project_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )
        batch_op.add_column(sa.Column("group_id", sa.String(length=36)))
        batch_op.create_foreign_key(
            "fk_data_stores_group_id",
            "project_groups",
            ["group_id"],
            ["group_id"],
            ondelete="CASCADE",
        )
        batch_op.create_unique_constraint(
            "uq_data_stores_group_name", ["group_id", "name"]
        )
    op.create_index(
        "ix_data_stores_group_default",
        "data_stores",
        ["group_id", "is_default"],
    )


def downgrade() -> None:
    op.drop_index("ix_data_stores_group_default", table_name="data_stores")
    with op.batch_alter_table("data_stores") as batch_op:
        batch_op.drop_constraint("uq_data_stores_group_name", type_="unique")
        batch_op.drop_constraint("fk_data_stores_group_id", type_="foreignkey")
        batch_op.drop_column("group_id")
        batch_op.alter_column(
            "project_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
