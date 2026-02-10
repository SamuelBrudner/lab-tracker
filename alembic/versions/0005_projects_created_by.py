"""Add created_by column to projects.

Revision ID: 0005_projects_created_by
Revises: 0004_claims_visualizations
Create Date: 2026-02-07 18:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_projects_created_by"
down_revision = "0004_claims_visualizations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("created_by", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "created_by")
