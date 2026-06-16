"""Replace dataset_review_required with review_policy on projects.

Revision ID: 0008_project_review_policy
Revises: 0007_dataset_reviews
Create Date: 2026-02-09 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008_project_review_policy"
down_revision = "0007_dataset_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column(
                "review_policy",
                sa.String(length=20),
                nullable=False,
                server_default="none",
            )
        )

    # Backfill from the legacy boolean flag added in 0007_dataset_reviews.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "UPDATE projects SET review_policy = CASE "
            "WHEN dataset_review_required IS TRUE THEN 'all' "
            "ELSE 'none' "
            "END"
        )
    else:
        op.execute(
            "UPDATE projects SET review_policy = CASE "
            "WHEN dataset_review_required = 1 THEN 'all' "
            "ELSE 'none' "
            "END"
        )

    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("dataset_review_required")


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column(
                "dataset_review_required",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "UPDATE projects SET dataset_review_required = CASE "
            "WHEN review_policy = 'all' THEN TRUE "
            "ELSE FALSE "
            "END"
        )
    else:
        op.execute(
            "UPDATE projects SET dataset_review_required = CASE "
            "WHEN review_policy = 'all' THEN 1 "
            "ELSE 0 "
            "END"
        )

    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("review_policy")

