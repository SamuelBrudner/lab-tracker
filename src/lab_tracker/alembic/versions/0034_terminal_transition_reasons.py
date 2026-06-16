"""Capture terminal transition lessons."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0034_terminal_transition_reasons"
down_revision = "0033_persistent_invitations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("questions", sa.Column("terminal_reason", sa.Text(), nullable=True))
    op.add_column("datasets", sa.Column("terminal_reason", sa.Text(), nullable=True))
    op.add_column("analyses", sa.Column("terminal_reason", sa.Text(), nullable=True))
    op.add_column("claims", sa.Column("terminal_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("claims", "terminal_reason")
    op.drop_column("analyses", "terminal_reason")
    op.drop_column("datasets", "terminal_reason")
    op.drop_column("questions", "terminal_reason")
