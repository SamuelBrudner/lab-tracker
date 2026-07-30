"""Bind registered data stores to immutable operator authority grants.

Existing stores deliberately remain unbound: this revision adds nullable paired
binding columns and never infers authority from the broad resolver policy.

Revision ID: 0058_data_store_authority_bindings
Revises: 0057_review_email_outbox
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058_data_store_authority_bindings"
down_revision = "0057_review_email_outbox"
branch_labels = None
depends_on = None

_SCOPE_CONSTRAINT = "ck_data_stores_scope_xor"
_BINDING_PAIR_CONSTRAINT = "ck_data_stores_authority_binding_pair"
_GRANT_ID_FORMAT_CONSTRAINT = "ck_data_stores_authority_grant_id_format"
_FINGERPRINT_FORMAT_CONSTRAINT = "ck_data_stores_authority_fingerprint_format"
_SCOPE_SQL = (
    "((project_id IS NOT NULL AND group_id IS NULL) OR "
    "(project_id IS NULL AND group_id IS NOT NULL))"
)
_BINDING_PAIR_SQL = (
    "((authority_grant_id IS NULL AND authority_grant_fingerprint IS NULL) OR "
    "(authority_grant_id IS NOT NULL AND authority_grant_fingerprint IS NOT NULL))"
)
_DIAGNOSTIC_SAMPLE_LIMIT = 5

_GRANT_ID_COLUMN = sa.column("authority_grant_id", sa.String())
_FINGERPRINT_COLUMN = sa.column("authority_grant_fingerprint", sa.String())
_GRANT_ID_FIRST_CHARACTER_PATTERN = (
    r"[ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789]"
)
_GRANT_ID_INVALID_CHARACTER_PATTERN = (
    r"[^ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-]"
)
_GRANT_ID_FORMAT_EXPRESSION = sa.or_(
    _GRANT_ID_COLUMN.is_(None),
    sa.and_(
        sa.func.length(_GRANT_ID_COLUMN).between(1, 128),
        sa.func.substr(_GRANT_ID_COLUMN, 1, 1).regexp_match(
            _GRANT_ID_FIRST_CHARACTER_PATTERN
        ),
        sa.not_(
            _GRANT_ID_COLUMN.regexp_match(_GRANT_ID_INVALID_CHARACTER_PATTERN)
        ),
    ),
)
_FINGERPRINT_FORMAT_EXPRESSION = sa.or_(
    _FINGERPRINT_COLUMN.is_(None),
    sa.and_(
        sa.func.length(_FINGERPRINT_COLUMN) == 78,
        sa.func.substr(_FINGERPRINT_COLUMN, 1, 14) == "sag-v1-sha256:",
        sa.not_(
            sa.func.substr(_FINGERPRINT_COLUMN, 15).regexp_match(
                r"[^0123456789abcdef]"
            )
        ),
    ),
)


def upgrade() -> None:
    _acquire_writer_fence()
    _preflight_scope_xor()
    with op.batch_alter_table("data_stores") as batch_op:
        batch_op.add_column(
            sa.Column("authority_grant_id", sa.String(length=128), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "authority_grant_fingerprint",
                sa.String(length=78),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(_SCOPE_CONSTRAINT, _SCOPE_SQL)
        batch_op.create_check_constraint(
            _BINDING_PAIR_CONSTRAINT,
            _BINDING_PAIR_SQL,
        )
        batch_op.create_check_constraint(
            _GRANT_ID_FORMAT_CONSTRAINT,
            _GRANT_ID_FORMAT_EXPRESSION,
        )
        batch_op.create_check_constraint(
            _FINGERPRINT_FORMAT_CONSTRAINT,
            _FINGERPRINT_FORMAT_EXPRESSION,
        )


def downgrade() -> None:
    _acquire_writer_fence()
    with op.batch_alter_table("data_stores") as batch_op:
        batch_op.drop_constraint(_FINGERPRINT_FORMAT_CONSTRAINT, type_="check")
        batch_op.drop_constraint(_GRANT_ID_FORMAT_CONSTRAINT, type_="check")
        batch_op.drop_constraint(_BINDING_PAIR_CONSTRAINT, type_="check")
        batch_op.drop_constraint(_SCOPE_CONSTRAINT, type_="check")
        batch_op.drop_column("authority_grant_fingerprint")
        batch_op.drop_column("authority_grant_id")


def _acquire_writer_fence() -> None:
    """Fence concurrent writers before preflight and through the table change."""

    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        # Alembic uses transactional_ddl=False for SQLite and pysqlite does not
        # start a physical transaction for a read. Reserve the writer slot now.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    elif connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE data_stores IN ACCESS EXCLUSIVE MODE"))


def _preflight_scope_xor() -> None:
    """Abort with locator-free diagnostics when legacy scope rows are invalid."""

    connection = op.get_bind()
    invalid_scope_sql = (
        "NOT ((project_id IS NOT NULL AND group_id IS NULL) OR "
        "(project_id IS NULL AND group_id IS NOT NULL))"
    )
    samples = connection.execute(
        sa.text(
            "SELECT store_id, "
            "CASE WHEN project_id IS NULL AND group_id IS NULL "
            "THEN 'missing_scope' ELSE 'multiple_scopes' END AS violation "
            "FROM data_stores "
            f"WHERE {invalid_scope_sql} "
            "ORDER BY store_id "
            f"LIMIT {_DIAGNOSTIC_SAMPLE_LIMIT}"
        )
    ).all()
    if not samples:
        return

    violation_count = int(
        connection.scalar(
            sa.text(f"SELECT COUNT(*) FROM data_stores WHERE {invalid_scope_sql}")
        )
        or 0
    )
    rendered_samples = ", ".join(
        f"{row.store_id}={row.violation}" for row in samples
    )
    remaining_count = violation_count - len(samples)
    suffix = f", plus {remaining_count} more" if remaining_count > 0 else ""
    raise RuntimeError(
        "Cannot apply migration 0058_data_store_authority_bindings or create "
        f"{_SCOPE_CONSTRAINT}: found {violation_count} existing data-store row(s) "
        "without exactly one registration scope. Safe store_id=violation samples: "
        f"{rendered_samples}{suffix}. No scope or authority values were changed. "
        "Correct each row explicitly, then retry the migration."
    )
