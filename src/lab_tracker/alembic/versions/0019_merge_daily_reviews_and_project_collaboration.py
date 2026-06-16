"""Merge daily review and project collaboration migration branches."""

from __future__ import annotations

revision = "0019_merge_daily_reviews_and_project_collaboration"
down_revision = (
    "0017_daily_graph_reviews",
    "0018_project_memberships_graph_review",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
