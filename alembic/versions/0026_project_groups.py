"""Add project groups and group memberships."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0026_project_groups"
down_revision = "0025_dataset_external_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_groups",
        sa.Column("group_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("kind", sa.String(length=30), nullable=False, server_default="lab"),
        sa.Column("group_read_all", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("group_id"),
    )
    op.create_table(
        "group_memberships",
        sa.Column("membership_id", sa.String(length=36), nullable=False),
        sa.Column("group_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["group_id"], ["project_groups.group_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("membership_id"),
        sa.UniqueConstraint("group_id", "user_id", name="uq_group_memberships_group_user"),
    )
    op.create_index(
        "ix_group_memberships_user_group",
        "group_memberships",
        ["user_id", "group_id"],
    )
    op.create_index(
        "ix_group_memberships_group_role",
        "group_memberships",
        ["group_id", "role"],
    )
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("group_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_projects_group_id_project_groups",
            "project_groups",
            ["group_id"],
            ["group_id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_projects_group_id", "projects", ["group_id"])


def downgrade() -> None:
    op.drop_index("ix_projects_group_id", table_name="projects")
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_constraint("fk_projects_group_id_project_groups", type_="foreignkey")
        batch_op.drop_column("group_id")
    op.drop_index("ix_group_memberships_group_role", table_name="group_memberships")
    op.drop_index("ix_group_memberships_user_group", table_name="group_memberships")
    op.drop_table("group_memberships")
    op.drop_table("project_groups")
