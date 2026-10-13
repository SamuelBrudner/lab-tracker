"""Keep reviewer triage state separate from the immutable provider input."""

import sqlalchemy as sa
from alembic import op

revision = "0067_graph_draft_review_metadata"
down_revision = "0066_delegated_curation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "graph_change_sets",
        sa.Column(
            "review_metadata",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("graph_change_sets", "review_metadata")
