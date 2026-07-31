"""SQLAlchemy rows for acquisition collections.

The manifest payload intentionally lives in a one-to-one table separate from
snapshot summary metadata. Ordinary collection queries therefore cannot
accidentally load a 10,000-member JSON document.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from lab_tracker.db import Base
from lab_tracker.db_types import GUID, UtcDateTime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AcquisitionCollectionModel(Base):
    __tablename__ = "acquisition_collections"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "collection_key",
            name="uq_acquisition_collections_session_key",
        ),
        Index(
            "ix_acquisition_collections_session_created",
            "session_id",
            "created_at",
        ),
        CheckConstraint(
            "(current_snapshot_id IS NULL "
            "AND current_capture_id IS NULL "
            "AND current_observed_at IS NULL) "
            "OR (current_snapshot_id IS NOT NULL "
            "AND current_capture_id IS NOT NULL "
            "AND current_observed_at IS NOT NULL)",
            name="ck_acquisition_collections_current_pointer_complete",
        ),
        ForeignKeyConstraint(
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
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    collection_id: Mapped[UUID] = mapped_column(GUID, primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    collection_key: Mapped[str] = mapped_column(String(120), nullable=False)
    current_snapshot_id: Mapped[UUID | None] = mapped_column(GUID)
    current_capture_id: Mapped[UUID | None] = mapped_column(GUID)
    current_observed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        nullable=False,
        default=_utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
    )


class AcquisitionCollectionSnapshotModel(Base):
    __tablename__ = "acquisition_collection_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "collection_id",
            "manifest_hash",
            name="uq_collection_snapshots_collection_manifest",
        ),
        UniqueConstraint(
            "collection_id",
            "snapshot_id",
            name="uq_collection_snapshots_collection_snapshot",
        ),
        Index(
            "ix_collection_snapshots_collection_observed",
            "collection_id",
            "observed_at",
        ),
    )

    snapshot_id: Mapped[UUID] = mapped_column(GUID, primary_key=True, default=uuid4)
    collection_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("acquisition_collections.collection_id", ondelete="CASCADE"),
        nullable=False,
    )
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_provider: Mapped[str | None] = mapped_column(String(80))
    source_uri: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    sealed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    client_capture_id: Mapped[str] = mapped_column(String(200), nullable=False)
    capture_actor_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    capture_principal_type: Mapped[str | None] = mapped_column(String(32))
    capture_principal_instance_id: Mapped[UUID | None] = mapped_column(GUID)
    capture_principal_label: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        nullable=False,
        default=_utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
    )


class AcquisitionCollectionManifestModel(Base):
    __tablename__ = "acquisition_collection_manifests"

    snapshot_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("acquisition_collection_snapshots.snapshot_id", ondelete="CASCADE"),
        primary_key=True,
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    canonical_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        nullable=False,
        default=_utc_now,
    )


class AcquisitionCollectionCaptureModel(Base):
    __tablename__ = "acquisition_collection_captures"
    __table_args__ = (
        UniqueConstraint(
            "collection_id",
            "client_capture_id",
            name="uq_collection_captures_collection_client_id",
        ),
        UniqueConstraint(
            "collection_id",
            "snapshot_id",
            "capture_id",
            "observed_at",
            name="uq_collection_captures_collection_snapshot_capture",
        ),
        ForeignKeyConstraint(
            ["collection_id", "snapshot_id"],
            [
                "acquisition_collection_snapshots.collection_id",
                "acquisition_collection_snapshots.snapshot_id",
            ],
            name="fk_collection_captures_snapshot_owner",
            ondelete="CASCADE",
        ),
        Index("ix_collection_captures_snapshot_id", "snapshot_id"),
    )

    capture_id: Mapped[UUID] = mapped_column(GUID, primary_key=True, default=uuid4)
    collection_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("acquisition_collections.collection_id", ondelete="CASCADE"),
        nullable=False,
    )
    client_capture_id: Mapped[str] = mapped_column(String(200), nullable=False)
    snapshot_id: Mapped[UUID] = mapped_column(GUID, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_provider: Mapped[str | None] = mapped_column(String(80))
    source_uri: Mapped[str | None] = mapped_column(Text)
    capture_actor_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    capture_principal_type: Mapped[str | None] = mapped_column(String(32))
    capture_principal_instance_id: Mapped[UUID | None] = mapped_column(GUID)
    capture_principal_label: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        nullable=False,
        default=_utc_now,
    )
