"""Backfill attribution user foreign-key mirrors."""

from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa

from alembic import op

revision = "0028_backfill_attribution_user_fks"
down_revision = "0027_attribution_user_fks"
branch_labels = None
depends_on = None

LOCAL_AUTH_USER_ID = "00000000-0000-4000-8000-000000000001"

CREATED_BY_TABLES = (
    ("project_groups", "group_id"),
    ("projects", "project_id"),
    ("questions", "question_id"),
    ("question_refactors", "refactor_id"),
    ("datasets", "dataset_id"),
    ("notes", "note_id"),
    ("graph_change_sets", "change_set_id"),
    ("graph_draft_batch_runs", "run_id"),
    ("sessions", "session_id"),
    ("goals", "goal_id"),
    ("goal_links", "link_id"),
    ("project_memberships", "membership_id"),
    ("group_memberships", "membership_id"),
)


def upgrade() -> None:
    for table_name, primary_key in CREATED_BY_TABLES:
        _backfill_user_fk(
            table_name=table_name,
            primary_key=primary_key,
            source_column="created_by",
            target_column="created_by_user_id",
        )
    _backfill_user_fk(
        table_name="analyses",
        primary_key="analysis_id",
        source_column="executed_by",
        target_column="executed_by_user_id",
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table_name, _ in CREATED_BY_TABLES:
        bind.execute(sa.text(f"UPDATE {table_name} SET created_by_user_id = NULL"))
    bind.execute(sa.text("UPDATE analyses SET executed_by_user_id = NULL"))


def _backfill_user_fk(
    *,
    table_name: str,
    primary_key: str,
    source_column: str,
    target_column: str,
) -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            f"SELECT {primary_key}, {source_column} "
            f"FROM {table_name} "
            f"WHERE {source_column} IS NOT NULL "
            f"AND {target_column} IS NULL"
        )
    ).mappings()
    for row in rows:
        user_id = _valid_user_id(row[source_column])
        if user_id is None or not _user_exists(bind, user_id):
            continue
        bind.execute(
            sa.text(
                f"UPDATE {table_name} "
                f"SET {target_column} = :user_id "
                f"WHERE {primary_key} = :record_id"
            ),
            {"user_id": user_id, "record_id": row[primary_key]},
        )


def _valid_user_id(raw_value: object) -> str | None:
    try:
        user_id = str(UUID(str(raw_value)))
    except (TypeError, ValueError):
        return None
    if user_id == LOCAL_AUTH_USER_ID:
        return None
    return user_id


def _user_exists(bind, user_id: str) -> bool:
    return (
        bind.execute(
            sa.text("SELECT 1 FROM users WHERE user_id = :user_id"),
            {"user_id": user_id},
        ).first()
        is not None
    )
