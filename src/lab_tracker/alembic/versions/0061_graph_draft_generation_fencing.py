"""Fence graph-draft generation and batch-run recovery.

Revision ID: 0061_graph_draft_generation_fencing
Revises: 0060_acquisition_collections
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0061_graph_draft_generation_fencing"
down_revision = "0060_acquisition_collections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("graph_change_sets") as batch_op:
        batch_op.add_column(
            sa.Column("generation_claim_token", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "generation_claimed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "generation_lease_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "generation_attempt_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.create_check_constraint(
            "ck_graph_change_sets_generation_attempt_count",
            "generation_attempt_count >= 0",
        )
        batch_op.create_index(
            "ix_graph_change_sets_generation_lease",
            ["status", "generation_lease_expires_at"],
            unique=False,
        )

    with op.batch_alter_table("graph_draft_batch_runs") as batch_op:
        batch_op.add_column(sa.Column("claim_token", sa.String(length=36), nullable=True))
        batch_op.add_column(
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "attempt_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.create_check_constraint(
            "ck_graph_draft_batch_runs_attempt_count",
            "attempt_count >= 0",
        )
        batch_op.create_index(
            "ix_graph_draft_batch_runs_claimable",
            ["status", "lease_expires_at", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("graph_draft_batch_runs") as batch_op:
        batch_op.drop_index("ix_graph_draft_batch_runs_claimable")
        batch_op.drop_constraint(
            "ck_graph_draft_batch_runs_attempt_count",
            type_="check",
        )
        batch_op.drop_column("attempt_count")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("claimed_at")
        batch_op.drop_column("claim_token")

    with op.batch_alter_table("graph_change_sets") as batch_op:
        batch_op.drop_index("ix_graph_change_sets_generation_lease")
        batch_op.drop_constraint(
            "ck_graph_change_sets_generation_attempt_count",
            type_="check",
        )
        batch_op.drop_column("generation_attempt_count")
        batch_op.drop_column("generation_lease_expires_at")
        batch_op.drop_column("generation_claimed_at")
        batch_op.drop_column("generation_claim_token")
