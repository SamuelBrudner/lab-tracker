"""Track opt-in and proof of ownership for the scientist's login mailbox."""

import sqlalchemy as sa
from alembic import op

revision = "0068_verified_review_mailbox"
down_revision = "0067_graph_draft_review_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("review_mailbox_address", sa.String(150)))
    op.add_column("users", sa.Column("review_mailbox_verified_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("review_mailbox_challenge_hash", sa.String(64)))
    op.add_column(
        "users", sa.Column("review_mailbox_challenge_expires_at", sa.DateTime(timezone=True))
    )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("review_mailbox_challenge_expires_at")
        batch.drop_column("review_mailbox_challenge_hash")
        batch.drop_column("review_mailbox_verified_at")
        batch.drop_column("review_mailbox_address")
