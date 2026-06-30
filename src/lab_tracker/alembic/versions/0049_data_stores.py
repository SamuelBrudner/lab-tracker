"""Add data_stores table for the registered data-store registry."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0049_data_stores"
down_revision = "0048_provenance_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_stores",
        sa.Column("store_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("capabilities", sa.JSON()),
        sa.Column("root", sa.String(length=2000), nullable=False),
        sa.Column("endpoint", sa.String(length=2000)),
        sa.Column("credential_ref", sa.String(length=255)),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("created_by", sa.String(length=255)),
        sa.Column(
            "created_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.user_id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "name", name="uq_data_stores_project_name"),
    )
    op.create_index(
        "ix_data_stores_project_default",
        "data_stores",
        ["project_id", "is_default"],
    )


def downgrade() -> None:
    op.drop_index("ix_data_stores_project_default", table_name="data_stores")
    op.drop_table("data_stores")
