"""Protect Dataset collection snapshot references with relational edges."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0059_dataset_collection_snapshot_links"
down_revision = "0058_acquisition_collections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "datasets",
        sa.Column(
            "manifest_collection_snapshots",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.create_table(
        "dataset_collection_snapshots",
        sa.Column(
            "dataset_id",
            sa.String(length=36),
            sa.ForeignKey("datasets.dataset_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "snapshot_id",
            sa.String(length=36),
            sa.ForeignKey("acquisition_collection_snapshots.snapshot_id"),
            primary_key=True,
        ),
    )
    op.create_index(
        "ix_dataset_collection_snapshots_snapshot_id",
        "dataset_collection_snapshots",
        ["snapshot_id"],
    )
    _backfill_dataset_snapshot_links()


def downgrade() -> None:
    op.drop_index(
        "ix_dataset_collection_snapshots_snapshot_id",
        table_name="dataset_collection_snapshots",
    )
    op.drop_table("dataset_collection_snapshots")
    op.drop_column("datasets", "manifest_collection_snapshots")


def _backfill_dataset_snapshot_links() -> None:
    """Materialize integrity edges for compact references already persisted."""

    datasets = sa.table(
        "datasets",
        sa.column("dataset_id", sa.String(length=36)),
        sa.column("manifest_collection_snapshots", sa.JSON()),
    )
    snapshots = sa.table(
        "acquisition_collection_snapshots",
        sa.column("snapshot_id", sa.String(length=36)),
    )
    links = sa.table(
        "dataset_collection_snapshots",
        sa.column("dataset_id", sa.String(length=36)),
        sa.column("snapshot_id", sa.String(length=36)),
    )

    connection = op.get_bind()
    known_snapshot_ids = {
        str(value)
        for value in connection.execute(
            sa.select(snapshots.c.snapshot_id)
        ).scalars()
    }
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for dataset_id, raw_manifest in connection.execute(
        sa.select(
            datasets.c.dataset_id,
            datasets.c.manifest_collection_snapshots,
        )
    ):
        dataset_value = str(dataset_id)
        for reference in _manifest_references(raw_manifest, dataset_value):
            snapshot_id = reference.get("snapshot_id")
            if not isinstance(snapshot_id, str) or not snapshot_id.strip():
                raise RuntimeError(
                    "Cannot migrate Dataset collection references: "
                    f"Dataset {dataset_value} has a reference without snapshot_id."
                )
            snapshot_value = snapshot_id.strip()
            if snapshot_value not in known_snapshot_ids:
                raise RuntimeError(
                    "Cannot migrate dangling Dataset collection reference: "
                    f"Dataset {dataset_value} names missing snapshot "
                    f"{snapshot_value}."
                )
            key = (dataset_value, snapshot_value)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "dataset_id": dataset_value,
                    "snapshot_id": snapshot_value,
                }
            )
    if rows:
        connection.execute(sa.insert(links), rows)


def _manifest_references(
    raw_manifest: Any,
    dataset_id: str,
) -> Iterable[dict[str, Any]]:
    if raw_manifest in (None, ""):
        return []
    parsed = json.loads(raw_manifest) if isinstance(raw_manifest, str) else raw_manifest
    if not isinstance(parsed, list):
        raise RuntimeError(
            "Cannot migrate Dataset collection references: "
            f"Dataset {dataset_id} manifest_collection_snapshots is not a list."
        )
    if not all(isinstance(item, dict) for item in parsed):
        raise RuntimeError(
            "Cannot migrate Dataset collection references: "
            f"Dataset {dataset_id} has a malformed collection reference."
        )
    return parsed
