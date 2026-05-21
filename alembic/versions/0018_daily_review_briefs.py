"""Persist daily graph review briefs."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0018_daily_review_briefs"
down_revision = "0017_daily_graph_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("daily_graph_reviews") as batch_op:
        batch_op.add_column(sa.Column("review_brief", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("daily_graph_reviews") as batch_op:
        batch_op.drop_column("review_brief")
