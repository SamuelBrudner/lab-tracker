"""Add dataset reviews and review-required project policy.

Revision ID: 0007_dataset_reviews
Revises: 0006_dataset_files
Create Date: 2026-02-09 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0007_dataset_reviews"
down_revision = "0006_dataset_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "dataset_review_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_table(
        "dataset_reviews",
        sa.Column("review_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.String(length=36),
            sa.ForeignKey("datasets.dataset_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reviewer_user_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("comments", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_dataset_reviews_dataset_id",
        "dataset_reviews",
        ["dataset_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_dataset_reviews_dataset_id", table_name="dataset_reviews")
    op.drop_table("dataset_reviews")
    op.drop_column("projects", "dataset_review_required")

