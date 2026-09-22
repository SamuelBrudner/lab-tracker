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
    _acquire_sqlite_write_lock()
    _preflight_existing_confidence_values()
    with op.batch_alter_table("claims") as batch_op:
        batch_op.create_check_constraint(
            _CONSTRAINT_NAME,
            _CONSTRAINT_SQL,
        )


def downgrade() -> None:
    with op.batch_alter_table("claims") as batch_op:
        batch_op.drop_constraint(_CONSTRAINT_NAME, type_="check")


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

    A writer that committed an invalid value between the clean preflight read
    and the batch copy would otherwise cause a raw constraint error.  env.py
    runs every SQLite migration inside one ``BEGIN IMMEDIATE`` transaction, so
    the writer reservation is normally already held here; this helper only
    starts one when the revision runs on a connection without a physical
    transaction (for example when exercised directly by a test).
    """

    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
