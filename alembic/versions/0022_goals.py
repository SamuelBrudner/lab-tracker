"""Goals and goal links.

Revision ID: 0022_goals
Revises: 0021_claim_questions
Create Date: 2026-06-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0022_goals"
down_revision = "0021_claim_questions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "goals",
        sa.Column("goal_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("goal_type", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("external_ref", sa.String(length=1000), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("goal_id"),
    )
    op.create_index(
        "ix_goals_project_created_at",
        "goals",
        ["project_id", "created_at"],
    )
    op.create_table(
        "goal_links",
        sa.Column("link_id", sa.String(length=36), nullable=False),
        sa.Column("goal_id", sa.String(length=36), nullable=False),
        sa.Column("entity_type", sa.String(length=30), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("relation", sa.String(length=40), nullable=False),
        sa.Column("link_status", sa.String(length=20), nullable=True),
        sa.Column("slot", sa.String(length=120), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.goal_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("link_id"),
        sa.UniqueConstraint(
            "goal_id",
            "entity_type",
            "entity_id",
            "relation",
            "slot",
            name="uq_goal_links_goal_entity_relation_slot",
        ),
    )
    op.create_index(
        "ix_goal_links_entity_lookup",
        "goal_links",
        ["entity_type", "entity_id", "goal_id"],
    )
    op.create_index(
        "ix_goal_links_goal_status",
        "goal_links",
        ["goal_id", "link_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_goal_links_goal_status", table_name="goal_links")
    op.drop_index("ix_goal_links_entity_lookup", table_name="goal_links")
    op.drop_table("goal_links")
    op.drop_index("ix_goals_project_created_at", table_name="goals")
    op.drop_table("goals")
