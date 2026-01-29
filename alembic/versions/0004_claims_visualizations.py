"""Claims and visualizations schema.

Revision ID: 0004_claims_visualizations
Revises: 0003_entity_tag_suggestions
Create Date: 2026-01-29 21:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_claims_visualizations"
down_revision = "0003_entity_tag_suggestions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "claims",
        sa.Column("claim_id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="proposed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
    )
    op.create_table(
        "visualizations",
        sa.Column("viz_id", sa.String(length=36), primary_key=True),
        sa.Column("analysis_id", sa.String(length=36), nullable=False),
        sa.Column("viz_type", sa.String(length=40), nullable=False),
        sa.Column("file_path", sa.String(length=1000), nullable=False),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.analysis_id"], ondelete="CASCADE"),
    )
    op.create_table(
        "claim_datasets",
        sa.Column("claim_id", sa.String(length=36), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.claim_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.dataset_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("claim_id", "dataset_id"),
    )
    op.create_table(
        "claim_analyses",
        sa.Column("claim_id", sa.String(length=36), nullable=False),
        sa.Column("analysis_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.claim_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.analysis_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("claim_id", "analysis_id"),
    )
    op.create_table(
        "visualization_claims",
        sa.Column("viz_id", sa.String(length=36), nullable=False),
        sa.Column("claim_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["viz_id"], ["visualizations.viz_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.claim_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("viz_id", "claim_id"),
    )


def downgrade() -> None:
    op.drop_table("visualization_claims")
    op.drop_table("claim_analyses")
    op.drop_table("claim_datasets")
    op.drop_table("visualizations")
    op.drop_table("claims")
