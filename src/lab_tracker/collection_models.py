"""Read models for acquisition collections and their immutable snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _CollectionModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AcquisitionCollectionMember(_CollectionModel):
    """One member fact loaded only from a collection manifest."""

    path: str
    checksum: str
    size_bytes: int


class AcquisitionCollectionManifest(_CollectionModel):
    """The full managed manifest, returned only by explicit manifest reads."""

    schema_version: int = 1
    members: list[AcquisitionCollectionMember] = Field(default_factory=list)


class AcquisitionCollectionSnapshot(_CollectionModel):
    """Compact immutable content snapshot metadata."""

    snapshot_id: UUID
    collection_id: UUID
    manifest_hash: str
    member_count: int
    total_size_bytes: int
    complete: bool = False
    source_provider: str | None = None
    source_uri: str | None = None
    observed_at: datetime
    sealed_at: datetime | None = None
    client_capture_id: str
    capture_actor_user_id: UUID | None = None
    capture_principal_type: str | None = None
    capture_principal_instance_id: UUID | None = None
    capture_principal_label: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class AcquisitionCollection(_CollectionModel):
    """Logical, watch-configured collection owned by one Session."""

    collection_id: UUID
    session_id: UUID
    collection_key: str
    current_snapshot_id: UUID | None = None
    current_capture_id: UUID | None = None
    current_observed_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class AcquisitionCollectionSummary(AcquisitionCollection):
    """Collection list item with its compact current-snapshot metadata."""

    current_snapshot: AcquisitionCollectionSnapshot | None = None


class AcquisitionCollectionCapture(_CollectionModel):
    """Durable observation and idempotency receipt for one capture request."""

    capture_id: UUID
    collection_id: UUID
    client_capture_id: str
    snapshot_id: UUID
    request_hash: str
    observed_at: datetime
    complete: bool
    source_provider: str | None = None
    source_uri: str | None = None
    capture_actor_user_id: UUID | None = None
    capture_principal_type: str | None = None
    capture_principal_instance_id: UUID | None = None
    capture_principal_label: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)


class AcquisitionCollectionCaptureResult(_CollectionModel):
    """Result of applying one idempotent capture observation."""

    collection: AcquisitionCollection
    snapshot: AcquisitionCollectionSnapshot
    snapshot_reused: bool = False
    current_pointer_changed: bool = False


class DatasetCollectionSnapshotReference(_CollectionModel):
    """Compact collection provenance committed into a Dataset manifest."""

    snapshot_id: UUID
    collection_id: UUID
    collection_key: str
    manifest_hash: str
    member_count: int
    total_size_bytes: int
    source_provider: str | None = None
    source_uri: str | None = None
    observed_at: datetime
    client_capture_id: str | None = None
    complete: bool | None = None
    capture_actor_user_id: UUID | None = None
    capture_principal_type: str | None = None
    capture_principal_instance_id: UUID | None = None
    capture_principal_label: str | None = None


def snapshot_with_capture_observation(
    snapshot: AcquisitionCollectionSnapshot,
    capture: AcquisitionCollectionCapture | None,
) -> AcquisitionCollectionSnapshot:
    """Overlay non-content observation facts without mutating the snapshot."""

    if capture is None:
        return snapshot
    if (
        capture.collection_id != snapshot.collection_id
        or capture.snapshot_id != snapshot.snapshot_id
    ):
        raise ValueError("Collection capture does not belong to the snapshot.")
    return snapshot.model_copy(
        update={
            "complete": snapshot.complete,
            "source_provider": capture.source_provider,
            "source_uri": capture.source_uri,
            "observed_at": capture.observed_at,
            "client_capture_id": capture.client_capture_id,
            "capture_actor_user_id": capture.capture_actor_user_id,
            "capture_principal_type": capture.capture_principal_type,
            "capture_principal_instance_id": capture.capture_principal_instance_id,
            "capture_principal_label": capture.capture_principal_label,
        }
    )


class DatasetSummary(_CollectionModel):
    """Bounded Dataset list response that omits the full commit manifest."""

    dataset_id: UUID
    project_id: UUID
    commit_hash: str
    primary_question_id: UUID
    question_links: list[dict[str, object]] = Field(default_factory=list)
    status: str
    source_session_id: UUID | None = None
    file_count: int = 0
    external_artifact_count: int = 0
    collection_count: int = 0
    collection_member_count: int = 0
    collection_total_size_bytes: int = 0
    collection_snapshots: list[DatasetCollectionSnapshotReference] = Field(
        default_factory=list
    )
    created_at: datetime
    updated_at: datetime
