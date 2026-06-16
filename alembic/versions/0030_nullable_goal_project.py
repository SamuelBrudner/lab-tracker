"""Allow projectless spanning goals.

Downgrading this revision is intentionally one-way once projectless goals exist:
restoring the NOT NULL constraint on goals.project_id will fail until those rows
are reassigned or removed.

Revision ID: 0030_nullable_goal_project
Revises: 0029_supervision_edges
Create Date: 2026-06-15 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0030_nullable_goal_project"
down_revision = "0029_supervision_edges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("goals") as batch_op:
        batch_op.alter_column(
            "project_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("goals") as batch_op:
        batch_op.alter_column(
            "project_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
