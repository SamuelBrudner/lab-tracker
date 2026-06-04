"""Claim answers-question links.

Revision ID: 0021_claim_questions
Revises: 0020_visualization_assets
Create Date: 2026-06-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0021_claim_questions"
down_revision = "0020_visualization_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "claim_questions",
        sa.Column("claim_id", sa.String(length=36), nullable=False),
        sa.Column("question_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.claim_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["question_id"], ["questions.question_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("claim_id", "question_id"),
    )


def downgrade() -> None:
    op.drop_table("claim_questions")
