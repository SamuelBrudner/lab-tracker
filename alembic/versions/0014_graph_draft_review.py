"""Add graph draft review tables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0014_graph_draft_review"
down_revision = "0013_retained_v1_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "graph_change_sets",
        sa.Column("change_set_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_note_id", sa.String(length=36), nullable=False),
        sa.Column("source_checksum", sa.String(length=64), nullable=True),
        sa.Column("source_content_type", sa.String(length=255), nullable=True),
        sa.Column("source_filename", sa.String(length=255), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("commit_message", sa.Text(), nullable=True),
        sa.Column("error_metadata", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("committed_by", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_note_id"], ["notes.note_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("change_set_id"),
    )
    op.create_index(
        "ix_graph_change_sets_project_created_at",
        "graph_change_sets",
        ["project_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_graph_change_sets_note_created_at",
        "graph_change_sets",
        ["source_note_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "graph_change_operations",
        sa.Column("operation_id", sa.String(length=36), nullable=False),
        sa.Column("change_set_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("op", sa.String(length=20), nullable=False),
        sa.Column("entity_type", sa.String(length=30), nullable=False),
        sa.Column("target_entity_id", sa.String(length=36), nullable=True),
        sa.Column("client_ref", sa.String(length=80), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source_refs", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result_entity_id", sa.String(length=36), nullable=True),
        sa.Column("error_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["graph_change_sets.change_set_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("operation_id"),
        sa.UniqueConstraint(
            "change_set_id",
            "sequence",
            name="uq_graph_change_operations_change_set_sequence",
        ),
    )
    op.create_index(
        "ix_graph_change_operations_change_set_sequence",
        "graph_change_operations",
        ["change_set_id", "sequence"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_graph_change_operations_change_set_sequence",
        table_name="graph_change_operations",
    )
    op.drop_table("graph_change_operations")
    op.drop_index("ix_graph_change_sets_note_created_at", table_name="graph_change_sets")
    op.drop_index("ix_graph_change_sets_project_created_at", table_name="graph_change_sets")
    op.drop_table("graph_change_sets")
