"""HTTP request schemas for acquisition collection capture."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StrictInt

from lab_tracker.collection_manifest import MAX_COLLECTION_MEMBERS
from lab_tracker.schemas import NonBlankStr, RequestModel


class AcquisitionCollectionMemberInput(RequestModel):
    path: Annotated[str, Field(min_length=1, max_length=1_000)]
    checksum: Annotated[str, Field(min_length=64, max_length=64)]
    size_bytes: Annotated[StrictInt, Field(ge=0)]


class AcquisitionCollectionManifestInput(RequestModel):
    schema_version: Literal[1]
    members: Annotated[
        list[AcquisitionCollectionMemberInput],
        Field(max_length=MAX_COLLECTION_MEMBERS),
    ]


class AcquisitionCollectionSnapshotCreate(RequestModel):
    client_capture_id: Annotated[NonBlankStr, Field(max_length=200)]
    observed_at: datetime
    source_provider: Annotated[str, Field(max_length=80)] | None = None
    source_uri: Annotated[str, Field(max_length=2_000)] | None = None
    complete: bool
    manifest: AcquisitionCollectionManifestInput
