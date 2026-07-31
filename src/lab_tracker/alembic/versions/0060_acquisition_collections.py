"""Add acquisition collections and immutable snapshot manifests.

Revision ID: 0060_acquisition_collections
Revises: 0059_experiments
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0060_acquisition_collections"
down_revision = "0059_experiments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sqlite = op.get_context().dialect.name == "sqlite"
    collection_constraints: list[sa.SchemaItem] = [
        sa.UniqueConstraint(
            "session_id",
            "collection_key",
            name="uq_acquisition_collections_session_key",
        ),
        sa.CheckConstraint(
            "(current_snapshot_id IS NULL "
            "AND current_capture_id IS NULL "
            "AND current_observed_at IS NULL) "
            "OR (current_snapshot_id IS NOT NULL "
            "AND current_capture_id IS NOT NULL "
            "AND current_observed_at IS NOT NULL)",
            name="ck_acquisition_collections_current_pointer_complete",
        ),
    ]
    if sqlite:
        # SQLite accepts a forward reference and must receive this constraint
        # inline because it cannot add foreign keys with ALTER TABLE.
        collection_constraints.append(
            sa.ForeignKeyConstraint(
                [
                    "collection_id",
                    "current_snapshot_id",
                    "current_capture_id",
                    "current_observed_at",
                ],
                [
                    "acquisition_collection_captures.collection_id",
                    "acquisition_collection_captures.snapshot_id",
                    "acquisition_collection_captures.capture_id",
                    "acquisition_collection_captures.observed_at",
                ],
                name="fk_acquisition_collections_current_capture",
                deferrable=True,
                initially="DEFERRED",
            )
        )
    op.create_table(
        "acquisition_collections",
        sa.Column("collection_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("collection_key", sa.String(length=120), nullable=False),
        sa.Column("current_snapshot_id", sa.String(length=36)),
        sa.Column("current_capture_id", sa.String(length=36)),
        sa.Column("current_observed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        *collection_constraints,
    )
    op.create_index(
        "ix_acquisition_collections_session_created",
        "acquisition_collections",
        ["session_id", "created_at"],
    )

    op.create_table(
        "acquisition_collection_snapshots",
        sa.Column("snapshot_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "collection_id",
            sa.String(length=36),
            sa.ForeignKey(
                "acquisition_collections.collection_id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("total_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("source_provider", sa.String(length=80)),
        sa.Column("source_uri", sa.Text()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True)),
        sa.Column("client_capture_id", sa.String(length=200), nullable=False),
        sa.Column(
            "capture_actor_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.user_id", ondelete="SET NULL"),
        ),
        sa.Column("capture_principal_type", sa.String(length=32)),
        sa.Column("capture_principal_instance_id", sa.String(length=36)),
        sa.Column("capture_principal_label", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "collection_id",
            "manifest_hash",
            name="uq_collection_snapshots_collection_manifest",
        ),
        sa.UniqueConstraint(
            "collection_id",
            "snapshot_id",
            name="uq_collection_snapshots_collection_snapshot",
        ),
    )
    op.create_index(
        "ix_collection_snapshots_collection_observed",
        "acquisition_collection_snapshots",
        ["collection_id", "observed_at"],
    )

    op.create_table(
        "acquisition_collection_manifests",
        sa.Column(
            "snapshot_id",
            sa.String(length=36),
            sa.ForeignKey(
                "acquisition_collection_snapshots.snapshot_id",
                ondelete="CASCADE",
            ),
            primary_key=True,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("canonical_size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "acquisition_collection_captures",
        sa.Column("capture_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "collection_id",
            sa.String(length=36),
            sa.ForeignKey(
                "acquisition_collections.collection_id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("client_capture_id", sa.String(length=200), nullable=False),
        sa.Column(
            "snapshot_id",
            sa.String(length=36),
            nullable=False,
        ),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("source_provider", sa.String(length=80)),
        sa.Column("source_uri", sa.Text()),
        sa.Column(
            "capture_actor_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.user_id", ondelete="SET NULL"),
        ),
        sa.Column("capture_principal_type", sa.String(length=32)),
        sa.Column("capture_principal_instance_id", sa.String(length=36)),
        sa.Column("capture_principal_label", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "collection_id",
            "client_capture_id",
            name="uq_collection_captures_collection_client_id",
        ),
        sa.UniqueConstraint(
            "collection_id",
            "snapshot_id",
            "capture_id",
            "observed_at",
            name="uq_collection_captures_collection_snapshot_capture",
        ),
        sa.ForeignKeyConstraint(
            ["collection_id", "snapshot_id"],
            [
                "acquisition_collection_snapshots.collection_id",
                "acquisition_collection_snapshots.snapshot_id",
            ],
            name="fk_collection_captures_snapshot_owner",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_collection_captures_snapshot_id",
        "acquisition_collection_captures",
        ["snapshot_id"],
    )
    if not sqlite:
        op.create_foreign_key(
            "fk_acquisition_collections_current_capture",
            "acquisition_collections",
            "acquisition_collection_captures",
            [
                "collection_id",
                "current_snapshot_id",
                "current_capture_id",
                "current_observed_at",
            ],
            ["collection_id", "snapshot_id", "capture_id", "observed_at"],
            deferrable=True,
            initially="DEFERRED",
        )


def downgrade() -> None:
    sqlite = op.get_context().dialect.name == "sqlite"
    _set_sqlite_foreign_keys(enabled=False)
    try:
        if not sqlite:
            op.drop_constraint(
                "fk_acquisition_collections_current_capture",
                "acquisition_collections",
                type_="foreignkey",
            )
        op.drop_index(
            "ix_collection_captures_snapshot_id",
            table_name="acquisition_collection_captures",
        )
        op.drop_table("acquisition_collection_captures")
        op.drop_table("acquisition_collection_manifests")
        op.drop_index(
            "ix_collection_snapshots_collection_observed",
            table_name="acquisition_collection_snapshots",
        )
        op.drop_table("acquisition_collection_snapshots")
        op.drop_index(
            "ix_acquisition_collections_session_created",
            table_name="acquisition_collections",
        )
        op.drop_table("acquisition_collections")
    finally:
        _set_sqlite_foreign_keys(enabled=True)


def _set_sqlite_foreign_keys(*, enabled: bool) -> None:
    if op.get_context().dialect.name == "sqlite":
        value = "ON" if enabled else "OFF"
        op.execute(f"PRAGMA foreign_keys={value}")
