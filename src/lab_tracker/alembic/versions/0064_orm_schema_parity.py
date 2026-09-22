"""Enforce in the database the constraints the ORM metadata already declares.

Unit tests build their schema from ``lab_tracker.db_models`` while every
deployed database is built by these migrations, and the two had drifted:

* Fourteen columns that the application has written non-NULL ever since their
  table was created were left nullable by the migrations that created them.
  This revision makes the database enforce NOT NULL.  It never backfills: if
  any row holds NULL in one of these columns, the upgrade stops with a count
  and sample primary keys per column and changes nothing.
* ``notes.archived_by_user_id`` was added by 0046_curation_provenance without
  the ``users`` foreign key the ORM declares (every other attribution user id
  column has one).  The key is added with ``ON DELETE SET NULL``; archived-by
  values that name no user stop the upgrade the same way.

The remaining drift was resolved in the ORM instead: container columns that
earlier migrations added to populated tables without a backfill stay nullable,
and the seven indexes only migrations created are now declared on the models.
``tests/test_schema_parity.py`` keeps the two in lockstep from here on.

Revision ID: 0064_orm_schema_parity
Revises: 0063_user_session_epoch
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0064_orm_schema_parity"
down_revision = "0063_user_session_epoch"
branch_labels = None
depends_on = None

_DIAGNOSTIC_SAMPLE_LIMIT = 5
_ARCHIVED_BY_FOREIGN_KEY = "fk_notes_archived_by_user_id_users"
_TIMESTAMP = sa.DateTime(timezone=True)

# table -> (primary key column, ((column, existing type), ...))
_ColumnTypes = tuple[tuple[str, sa.types.TypeEngine[Any]], ...]
_NOT_NULL_COLUMNS: dict[str, tuple[str, _ColumnTypes]] = {
    "claim_edges": ("edge_id", (("created_at", _TIMESTAMP),)),
    "data_stores": (
        "store_id",
        (("capabilities", sa.JSON()), ("updated_at", _TIMESTAMP)),
    ),
    "entity_versions": (
        "version_id",
        (("snapshot", sa.JSON()), ("created_at", _TIMESTAMP)),
    ),
    "exploration_nodes": (
        "node_id",
        (("alternatives_considered", sa.JSON()), ("evidence_refs", sa.JSON())),
    ),
    "group_memberships": (
        "membership_id",
        (("created_at", _TIMESTAMP), ("updated_at", _TIMESTAMP)),
    ),
    "project_groups": (
        "group_id",
        (
            ("description", sa.String(length=1000)),
            ("created_at", _TIMESTAMP),
            ("updated_at", _TIMESTAMP),
        ),
    ),
    "supervision_edges": (
        "edge_id",
        (("created_at", _TIMESTAMP), ("updated_at", _TIMESTAMP)),
    ),
}

_ORPHANED_ARCHIVER_SQL = (
    "FROM notes WHERE archived_by_user_id IS NOT NULL AND NOT EXISTS "
    "(SELECT 1 FROM users WHERE users.user_id = notes.archived_by_user_id)"
)


def upgrade() -> None:
    _acquire_writer_fence()
    _preflight_not_null_columns()
    _preflight_archived_by_users()
    for table_name, (_primary_key, columns) in _NOT_NULL_COLUMNS.items():
        with op.batch_alter_table(table_name) as batch_op:
            for column_name, column_type in columns:
                batch_op.alter_column(
                    column_name,
                    existing_type=column_type,
                    nullable=False,
                )
    with op.batch_alter_table("notes") as batch_op:
        batch_op.create_foreign_key(
            _ARCHIVED_BY_FOREIGN_KEY,
            "users",
            ["archived_by_user_id"],
            ["user_id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    _acquire_writer_fence()
    with op.batch_alter_table("notes") as batch_op:
        batch_op.drop_constraint(_ARCHIVED_BY_FOREIGN_KEY, type_="foreignkey")
    for table_name, (_primary_key, columns) in reversed(_NOT_NULL_COLUMNS.items()):
        with op.batch_alter_table(table_name) as batch_op:
            for column_name, column_type in columns:
                batch_op.alter_column(
                    column_name,
                    existing_type=column_type,
                    nullable=True,
                )


def _acquire_writer_fence() -> None:
    """Keep writers out from the preflight reads through the schema change."""

    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        # env.py runs every SQLite migration inside one BEGIN IMMEDIATE
        # transaction, which already holds the writer reservation. Reserve it
        # here only when running on a connection without a physical transaction.
        driver_connection = connection.connection.driver_connection
        if driver_connection is None:
            raise RuntimeError("0064_orm_schema_parity needs an open SQLite connection.")
        if not driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    elif connection.dialect.name == "postgresql":
        # Take the locks the ALTERs need up front, in a fixed order, so no
        # writer can add a NULL or an orphaned archiver after the preflight.
        for table_name in sorted({*_NOT_NULL_COLUMNS, "notes"}):
            connection.execute(sa.text(f"LOCK TABLE {table_name} IN ACCESS EXCLUSIVE MODE"))
        connection.execute(sa.text("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE"))


def _preflight_not_null_columns() -> None:
    connection = op.get_bind()
    violations: list[str] = []
    for table_name, (primary_key, columns) in _NOT_NULL_COLUMNS.items():
        for column_name, _column_type in columns:
            null_rows = f"FROM {table_name} WHERE {column_name} IS NULL"
            samples = connection.execute(
                sa.text(
                    f"SELECT {primary_key} {null_rows} "  # noqa: S608 - constant identifiers
                    f"ORDER BY {primary_key} LIMIT {_DIAGNOSTIC_SAMPLE_LIMIT}"
                )
            ).scalars().all()
            if not samples:
                continue
            count = int(connection.scalar(sa.text(f"SELECT COUNT(*) {null_rows}")) or 0)
            rendered = ", ".join(f"{primary_key}={value}" for value in samples)
            remaining = count - len(samples)
            suffix = f", plus {remaining} more" if remaining > 0 else ""
            violations.append(f"{table_name}.{column_name}: {count} row(s) ({rendered}{suffix})")
    if not violations:
        return
    raise RuntimeError(
        "Cannot apply migration 0064_orm_schema_parity: the application always "
        "writes these columns, but existing rows hold NULL, so NOT NULL cannot be "
        f"enforced. {'; '.join(violations)}. No rows were changed, and this "
        "migration never backfills values. Set each value explicitly (for "
        "example from a backup or the record's history), then retry the migration."
    )


def _preflight_archived_by_users() -> None:
    connection = op.get_bind()
    samples = connection.execute(
        sa.text(
            f"SELECT note_id, archived_by_user_id {_ORPHANED_ARCHIVER_SQL} "  # noqa: S608
            f"ORDER BY note_id LIMIT {_DIAGNOSTIC_SAMPLE_LIMIT}"
        )
    ).all()
    if not samples:
        return
    count = int(connection.scalar(sa.text(f"SELECT COUNT(*) {_ORPHANED_ARCHIVER_SQL}")) or 0)
    rendered = ", ".join(f"note_id={row[0]} -> {row[1]}" for row in samples)
    remaining = count - len(samples)
    suffix = f", plus {remaining} more" if remaining > 0 else ""
    raise RuntimeError(
        "Cannot apply migration 0064_orm_schema_parity or create "
        f"{_ARCHIVED_BY_FOREIGN_KEY}: notes.archived_by_user_id names a user that "
        f"does not exist in {count} note(s) ({rendered}{suffix}). No rows were "
        "changed. Restore the missing users, or clear archived_by_user_id on those "
        "notes after recording who archived them, then retry the migration."
    )
