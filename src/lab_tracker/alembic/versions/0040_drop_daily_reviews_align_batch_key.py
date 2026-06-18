"""Drop dead daily reviews and align batch key uniqueness."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0040_drop_daily_reviews_align_batch_key"
down_revision = "0039_not_null_tightening_0014_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _replace_batch_key_index_with_constraint()
    op.drop_table("daily_graph_review_change_sets")
    op.drop_table("daily_graph_reviews")


def downgrade() -> None:
    _replace_batch_key_constraint_with_index()
    _create_daily_graph_review_tables()


def _replace_batch_key_index_with_constraint() -> None:
    op.drop_index("ix_graph_change_sets_batch_key", table_name="graph_change_sets")
    _set_sqlite_foreign_keys(enabled=False)
    try:
        with op.batch_alter_table("graph_change_sets") as batch_op:
            batch_op.create_unique_constraint(
                "uq_graph_change_sets_batch_key",
                ["batch_key"],
            )
    finally:
        _set_sqlite_foreign_keys(enabled=True)


def _replace_batch_key_constraint_with_index() -> None:
    _set_sqlite_foreign_keys(enabled=False)
    try:
        with op.batch_alter_table("graph_change_sets") as batch_op:
            batch_op.drop_constraint(
                "uq_graph_change_sets_batch_key",
                type_="unique",
            )
    finally:
        _set_sqlite_foreign_keys(enabled=True)
    op.create_index(
        "ix_graph_change_sets_batch_key",
        "graph_change_sets",
        ["batch_key"],
        unique=True,
    )


def _set_sqlite_foreign_keys(*, enabled: bool) -> None:
    if op.get_context().dialect.name == "sqlite":
        value = "ON" if enabled else "OFF"
        op.execute(f"PRAGMA foreign_keys={value}")


def _create_daily_graph_review_tables() -> None:
    op.create_table(
        "daily_graph_reviews",
        sa.Column("review_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("error_metadata", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("review_id"),
        sa.UniqueConstraint(
            "project_id",
            "window_start",
            "window_end",
            name="uq_daily_graph_reviews_project_window",
        ),
    )
    op.create_index(
        "ix_daily_graph_reviews_project_window_end",
        "daily_graph_reviews",
        ["project_id", "window_end"],
        unique=False,
    )
    op.create_index(
        "ix_daily_graph_reviews_status_window_end",
        "daily_graph_reviews",
        ["status", "window_end"],
        unique=False,
    )
    op.create_table(
        "daily_graph_review_change_sets",
        sa.Column("review_id", sa.String(length=36), nullable=False),
        sa.Column("change_set_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["graph_change_sets.change_set_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["daily_graph_reviews.review_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("review_id", "change_set_id"),
        sa.UniqueConstraint(
            "review_id",
            "sequence",
            name="uq_daily_graph_review_change_sets_review_sequence",
        ),
    )
    op.create_index(
        "ix_daily_graph_review_change_sets_change_set_id",
        "daily_graph_review_change_sets",
        ["change_set_id"],
        unique=False,
    )
