"""Add graph context metadata to graph drafts."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0015_graph_context_draft_metadata"
down_revision = "0014_graph_draft_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "graph_change_sets",
        sa.Column(
            "draft_mode",
            sa.String(length=30),
            nullable=False,
            server_default="graph_context",
        ),
    )
    op.add_column(
        "graph_change_sets",
        sa.Column("context_packet", sa.JSON(), nullable=True),
    )
    op.add_column(
        "graph_change_sets",
        sa.Column("summary", sa.Text(), nullable=True),
    )
    op.add_column(
        "graph_change_sets",
        sa.Column("uncertain_fields", sa.JSON(), nullable=True),
    )
    op.add_column(
        "graph_change_sets",
        sa.Column("clarification_requests", sa.JSON(), nullable=True),
    )
    op.add_column(
        "graph_change_operations",
        sa.Column("semantic_type", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("graph_change_operations", "semantic_type")
    op.drop_column("graph_change_sets", "clarification_requests")
    op.drop_column("graph_change_sets", "uncertain_fields")
    op.drop_column("graph_change_sets", "summary")
    op.drop_column("graph_change_sets", "context_packet")
    op.drop_column("graph_change_sets", "draft_mode")
