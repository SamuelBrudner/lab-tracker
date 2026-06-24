"""Merge ARA exploration and curation provenance migration branches."""

from __future__ import annotations

revision = "0047_merge_ara_and_curation_provenance"
down_revision = (
    "0046_ara_exploration_graph",
    "0046_curation_provenance",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
