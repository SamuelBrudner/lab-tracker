"""Enforce the claim confidence bound at the database boundary.

Claim confidence is an inclusive 0-to-100 value.  The application has long
validated that policy, but the table itself still accepted invalid values from
direct persistence paths.  This migration refuses to add the constraint when
legacy rows violate it: scientific values must be corrected explicitly rather
than silently clamped or otherwise rewritten.

Revision ID: 0056_claim_confidence_bounds
Revises: 0055_evidence_bundles
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0056_claim_confidence_bounds"
down_revision = "0055_evidence_bundles"
branch_labels = None
depends_on = None

_CONSTRAINT_NAME = "ck_claims_confidence_range"
_CONSTRAINT_SQL = "confidence >= 0 AND confidence <= 100"
_INVALID_CONFIDENCE_SQL = (
    "confidence IS NULL OR NOT (confidence >= 0 AND confidence <= 100)"
)
_DIAGNOSTIC_SAMPLE_LIMIT = 5


def upgrade() -> None:
    _set_sqlite_foreign_keys(enabled=False)
    try:
        _acquire_sqlite_write_lock()
        _preflight_existing_confidence_values()
        with op.batch_alter_table("claims") as batch_op:
            batch_op.create_check_constraint(
                _CONSTRAINT_NAME,
                _CONSTRAINT_SQL,
            )
    finally:
        _set_sqlite_foreign_keys(enabled=True)


def downgrade() -> None:
    _set_sqlite_foreign_keys(enabled=False)
    try:
        with op.batch_alter_table("claims") as batch_op:
            batch_op.drop_constraint(_CONSTRAINT_NAME, type_="check")
    finally:
        _set_sqlite_foreign_keys(enabled=True)


def _preflight_existing_confidence_values() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        # Acquire the same exclusive lock ALTER TABLE will need before checking
        # existing rows. This closes the preflight-to-DDL writer race without a
        # lock upgrade after another writer has queued behind the migration.
        connection.execute(sa.text("LOCK TABLE claims IN ACCESS EXCLUSIVE MODE"))

    samples = connection.execute(
        sa.text(
            "SELECT claim_id, confidence FROM claims "
            f"WHERE {_INVALID_CONFIDENCE_SQL} "
            "ORDER BY claim_id "
            f"LIMIT {_DIAGNOSTIC_SAMPLE_LIMIT}"
        )
    ).all()
    if not samples:
        return

    violation_count = int(
        connection.scalar(
            sa.text(f"SELECT COUNT(*) FROM claims WHERE {_INVALID_CONFIDENCE_SQL}")
        )
        or 0
    )
    rendered_samples = ", ".join(
        f"{row.claim_id}={row.confidence!r}" for row in samples
    )
    remaining_count = violation_count - len(samples)
    sample_suffix = (
        f", plus {remaining_count} more" if remaining_count > 0 else ""
    )
    raise RuntimeError(
        "Cannot apply migration 0056_claim_confidence_bounds or create "
        f"{_CONSTRAINT_NAME}: found {violation_count} existing claim row(s) "
        "with confidence outside the inclusive [0, 100] range. "
        f"Offending claim_id=confidence samples: {rendered_samples}{sample_suffix}. "
        "No confidence values were changed and this migration never clamps "
        "scientific values. Correct each value explicitly, then retry the migration."
    )


def _acquire_sqlite_write_lock() -> None:
    """Serialize SQLite writers from preflight through the table rebuild.

    Alembic deliberately configures SQLite with ``transactional_ddl=False``.
    With pysqlite's legacy transaction behavior, the read-only preflight would
    otherwise run before SQLite starts a physical transaction.  A writer could
    then commit an invalid value between that clean read and the batch copy,
    causing a raw constraint error and leaving Alembic's temporary table behind.

    ``BEGIN IMMEDIATE`` takes the database's writer reservation up front.  The
    logical SQLAlchemy/Alembic transaction already exists at this point, but no
    physical SQLite transaction has begun; Alembic commits or rolls it back
    together with the revision stamp.
    """

    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def _set_sqlite_foreign_keys(*, enabled: bool) -> None:
    if op.get_context().dialect.name == "sqlite":
        value = "ON" if enabled else "OFF"
        op.execute(f"PRAGMA foreign_keys={value}")
