"""Add project memberships and graph review attribution."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision = "0018_project_memberships_graph_review"
down_revision = "0017_device_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_memberships",
        sa.Column("membership_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("membership_id"),
        sa.UniqueConstraint(
            "project_id",
            "user_id",
            name="uq_project_memberships_project_user",
        ),
    )
    op.create_index(
        "ix_project_memberships_user_project",
        "project_memberships",
        ["user_id", "project_id"],
    )
    op.create_index(
        "ix_project_memberships_project_role",
        "project_memberships",
        ["project_id", "role"],
    )
    _backfill_project_owner_memberships()
    with op.batch_alter_table("graph_change_sets") as batch_op:
        batch_op.add_column(sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("submitted_by", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("reviewed_by", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("review_note", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("graph_change_sets") as batch_op:
        batch_op.drop_column("review_note")
        batch_op.drop_column("reviewed_by")
        batch_op.drop_column("reviewed_at")
        batch_op.drop_column("submitted_by")
        batch_op.drop_column("submitted_at")
    op.drop_index("ix_project_memberships_project_role", table_name="project_memberships")
    op.drop_index("ix_project_memberships_user_project", table_name="project_memberships")
    op.drop_table("project_memberships")


def _backfill_project_owner_memberships() -> None:
    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    projects = bind.execute(
        sa.text(
            "SELECT project_id, created_by, created_at "
            "FROM projects WHERE created_by IS NOT NULL"
        )
    ).mappings()
    for project in projects:
        user_id = str(project["created_by"])
        user_exists = bind.execute(
            sa.text("SELECT 1 FROM users WHERE user_id = :user_id"),
            {"user_id": user_id},
        ).first()
        if user_exists is None:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO project_memberships "
                "(membership_id, project_id, user_id, role, created_by, created_at, updated_at) "
                "VALUES "
                "(:membership_id, :project_id, :user_id, :role, :created_by, "
                ":created_at, :updated_at)"
            ),
            {
                "membership_id": str(uuid4()),
                "project_id": str(project["project_id"]),
                "user_id": user_id,
                "role": "owner",
                "created_by": user_id,
                "created_at": project["created_at"] or now,
                "updated_at": now,
            },
        )
