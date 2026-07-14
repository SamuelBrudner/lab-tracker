"""Add project and daily-review grants to invitations.

Revision ID: 0054_invitation_project_provisioning
Revises: 0053_capture_contexts_and_device_kind
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054_invitation_project_provisioning"
down_revision = "0053_capture_contexts_and_device_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("invitations") as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.String(length=36)))
        batch_op.add_column(sa.Column("project_role", sa.String(length=30)))
        batch_op.add_column(
            sa.Column(
                "review_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("review_cadence_minutes", sa.Integer()))
        batch_op.add_column(sa.Column("review_run_at_local_time", sa.String(length=5)))
        batch_op.add_column(sa.Column("review_timezone_name", sa.String(length=100)))
        batch_op.create_foreign_key(
            "fk_invitations_project_id_projects",
            "projects",
            ["project_id"],
            ["project_id"],
            ondelete="CASCADE",
        )
    op.create_index("ix_invitations_project_id", "invitations", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_invitations_project_id", table_name="invitations")
    with op.batch_alter_table("invitations") as batch_op:
        batch_op.drop_constraint(
            "fk_invitations_project_id_projects",
            type_="foreignkey",
        )
        batch_op.drop_column("review_timezone_name")
        batch_op.drop_column("review_run_at_local_time")
        batch_op.drop_column("review_cadence_minutes")
        batch_op.drop_column("review_enabled")
        batch_op.drop_column("project_role")
        batch_op.drop_column("project_id")
