"""Add provenance_links table for human-gated content-hash lineage proposals."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0048_provenance_links"
down_revision = "0047_merge_ara_and_curation_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provenance_links",
        sa.Column("link_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_entity_type", sa.String(length=40), nullable=False),
        sa.Column("source_entity_id", sa.String(length=36), nullable=False),
        sa.Column("target_entity_type", sa.String(length=40), nullable=False),
        sa.Column("target_entity_id", sa.String(length=36), nullable=False),
        sa.Column("relation", sa.String(length=32), nullable=False),
        sa.Column("basis", sa.String(length=32), nullable=False),
        sa.Column("content_hash", sa.String(length=64)),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="proposed"),
        sa.Column(
            "origin", sa.String(length=32), nullable=False, server_default="system_detected"
        ),
        sa.Column("acceptance_mode", sa.String(length=20)),
        sa.Column("accepted_by", sa.String(length=255)),
        sa.Column("accepted_by_user_id", sa.String(length=36)),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.String(length=255)),
        sa.Column("created_by_user_id", sa.String(length=36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "source_entity_id",
            "target_entity_id",
            "relation",
            name="uq_provenance_links_source_target_relation",
        ),
    )
    op.create_index(
        "ix_provenance_links_project_status",
        "provenance_links",
        ["project_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_provenance_links_project_status", table_name="provenance_links")
    op.drop_table("provenance_links")
