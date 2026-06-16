"""Add per-entity version snapshots."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_entity_versions"
down_revision = "0037_claim_logic_edges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entity_versions",
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("entity_type", sa.String(length=30), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=True),
        sa.Column("change_set_id", sa.String(length=36), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["graph_change_sets.change_set_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.user_id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("version_id"),
        sa.UniqueConstraint(
            "entity_type",
            "entity_id",
            "version_number",
            name="uq_entity_versions_entity_number",
        ),
    )
    op.create_index(
        "ix_entity_versions_entity_created_at",
        "entity_versions",
        ["entity_type", "entity_id", "created_at"],
    )
    op.create_index(
        "ix_entity_versions_change_set_id",
        "entity_versions",
        ["change_set_id"],
    )
    op.create_index(
        "ix_entity_versions_created_by_user_id",
        "entity_versions",
        ["created_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_entity_versions_created_by_user_id",
        table_name="entity_versions",
    )
    op.drop_index("ix_entity_versions_change_set_id", table_name="entity_versions")
    op.drop_index("ix_entity_versions_entity_created_at", table_name="entity_versions")
    op.drop_table("entity_versions")
