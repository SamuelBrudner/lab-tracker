"""Add the per-note disposition ledger to graph change sets.

Batch drafts must account for every staged note (bead lab-tracker-hymd);
the ledger persists the agent's note_dispositions plus server-generated
``not_presented`` rows for notes dropped before drafting.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055_graph_change_set_note_dispositions"
down_revision = "0054_invitation_project_provisioning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOT NULL with a server default, matching the 0039 tightening of the
    # sibling JSON columns so ORM metadata and schema stay aligned.
    op.add_column(
        "graph_change_sets",
        sa.Column(
            "note_dispositions",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("graph_change_sets", "note_dispositions")
