"""Add typed claim logic edges and external citations."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037_claim_logic_edges"
down_revision = "0036_analysis_external_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("claims") as batch_op:
        batch_op.add_column(sa.Column("external_citations", sa.JSON(), nullable=True))

    op.create_table(
        "claim_edges",
        sa.Column("edge_id", sa.String(length=36), nullable=False),
        sa.Column("claim_id", sa.String(length=36), nullable=False),
        sa.Column("target_claim_id", sa.String(length=36), nullable=False),
        sa.Column("relation", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.claim_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_claim_id"],
            ["claims.claim_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.user_id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("edge_id"),
        sa.UniqueConstraint(
            "claim_id",
            "target_claim_id",
            "relation",
            name="uq_claim_edges_claim_target_relation",
        ),
    )
    op.create_index("ix_claim_edges_claim_id", "claim_edges", ["claim_id"])
    op.create_index("ix_claim_edges_target_claim_id", "claim_edges", ["target_claim_id"])
    op.create_index(
        "ix_claim_edges_created_by_user_id",
        "claim_edges",
        ["created_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_claim_edges_created_by_user_id", table_name="claim_edges")
    op.drop_index("ix_claim_edges_target_claim_id", table_name="claim_edges")
    op.drop_index("ix_claim_edges_claim_id", table_name="claim_edges")
    op.drop_table("claim_edges")
    with op.batch_alter_table("claims") as batch_op:
        batch_op.drop_column("external_citations")
