"""Add dated supervision edges."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0029_supervision_edges"
down_revision = "0028_backfill_attribution_user_fks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "supervision_edges",
        sa.Column("edge_id", sa.String(length=36), nullable=False),
        sa.Column("supervisor_user_id", sa.String(length=36), nullable=False),
        sa.Column("supervisee_user_id", sa.String(length=36), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["supervisor_user_id"],
            ["users.user_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supervisee_user_id"],
            ["users.user_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("edge_id"),
    )
    op.create_index(
        "ix_supervision_edges_supervisor_started",
        "supervision_edges",
        ["supervisor_user_id", "started_at"],
    )
    op.create_index(
        "ix_supervision_edges_supervisee_started",
        "supervision_edges",
        ["supervisee_user_id", "started_at"],
    )
    op.create_index(
        "uq_supervision_edges_active_pair",
        "supervision_edges",
        ["supervisor_user_id", "supervisee_user_id"],
        unique=True,
        sqlite_where=sa.text("ended_at IS NULL"),
        postgresql_where=sa.text("ended_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_supervision_edges_active_pair", table_name="supervision_edges")
    op.drop_index("ix_supervision_edges_supervisee_started", table_name="supervision_edges")
    op.drop_index("ix_supervision_edges_supervisor_started", table_name="supervision_edges")
    op.drop_table("supervision_edges")
