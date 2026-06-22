"""Add ARA exploration nodes and claim falsification fields."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046_ara_exploration_graph"
down_revision = "0045_evening_default_run_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("claims") as batch_op:
        batch_op.add_column(sa.Column("falsification_criteria", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("verification_plan", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("refuting_outcome", sa.Text(), nullable=True))

    op.create_table(
        "exploration_nodes",
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("node_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("target_entity_type", sa.String(length=40), nullable=False),
        sa.Column("target_entity_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("choice", sa.Text(), nullable=True),
        sa.Column("alternatives_considered", sa.JSON(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("evidence_refs", sa.JSON(), nullable=True),
        sa.Column("hypothesis", sa.Text(), nullable=True),
        sa.Column("failure_mode", sa.Text(), nullable=True),
        sa.Column("lesson", sa.Text(), nullable=True),
        sa.Column("tooling_context", sa.Text(), nullable=True),
        sa.Column("trigger", sa.Text(), nullable=True),
        sa.Column("invalidates_node_id", sa.String(length=36), nullable=True),
        sa.Column("invalidates_claim_id", sa.String(length=36), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("change_set_id", sa.String(length=36), nullable=True),
        sa.Column("origin_provider", sa.String(length=80), nullable=True),
        sa.Column("origin_model", sa.String(length=255), nullable=True),
        sa.Column("origin_prompt_version", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["graph_change_sets.change_set_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["invalidates_claim_id"], ["claims.claim_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["invalidates_node_id"],
            ["exploration_nodes.node_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("node_id"),
    )
    op.create_table(
        "exploration_node_edges",
        sa.Column("edge_id", sa.String(length=36), nullable=False),
        sa.Column("source_node_id", sa.String(length=36), nullable=False),
        sa.Column("target_node_id", sa.String(length=36), nullable=False),
        sa.Column("relation", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_node_id"],
            ["exploration_nodes.node_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_node_id"],
            ["exploration_nodes.node_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("edge_id"),
        sa.UniqueConstraint(
            "source_node_id",
            "target_node_id",
            "relation",
            name="uq_exploration_node_edges_source_target_relation",
        ),
    )
    op.create_index(
        "ix_exploration_nodes_created_by_user_id",
        "exploration_nodes",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_exploration_nodes_change_set_id",
        "exploration_nodes",
        ["change_set_id"],
    )
    op.create_index(
        "ix_exploration_nodes_project_created_at",
        "exploration_nodes",
        ["project_id", "created_at"],
    )
    op.create_index("ix_exploration_nodes_type", "exploration_nodes", ["node_type"])
    op.create_index("ix_exploration_nodes_status", "exploration_nodes", ["status"])
    op.create_index(
        "ix_exploration_nodes_target",
        "exploration_nodes",
        ["target_entity_type", "target_entity_id"],
    )
    op.create_index(
        "ix_exploration_node_edges_source",
        "exploration_node_edges",
        ["source_node_id"],
    )
    op.create_index(
        "ix_exploration_node_edges_target",
        "exploration_node_edges",
        ["target_node_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_exploration_node_edges_target", table_name="exploration_node_edges")
    op.drop_index("ix_exploration_node_edges_source", table_name="exploration_node_edges")
    op.drop_index("ix_exploration_nodes_target", table_name="exploration_nodes")
    op.drop_index("ix_exploration_nodes_status", table_name="exploration_nodes")
    op.drop_index("ix_exploration_nodes_type", table_name="exploration_nodes")
    op.drop_index("ix_exploration_nodes_project_created_at", table_name="exploration_nodes")
    op.drop_index("ix_exploration_nodes_change_set_id", table_name="exploration_nodes")
    op.drop_index("ix_exploration_nodes_created_by_user_id", table_name="exploration_nodes")
    op.drop_table("exploration_node_edges")
    op.drop_table("exploration_nodes")
    with op.batch_alter_table("claims") as batch_op:
        batch_op.drop_column("refuting_outcome")
        batch_op.drop_column("verification_plan")
        batch_op.drop_column("falsification_criteria")
