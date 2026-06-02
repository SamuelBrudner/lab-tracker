"""PROV-O ingestion helpers for external artifacts."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lab_tracker.models import DatasetCommitManifestInput, DatasetFile

EXTERNAL_ARTIFACTS_METADATA_KEY = "external_artifacts"


class ExternalArtifactKind(str, Enum):
    ENTITY = "entity"
    ACTIVITY = "activity"


class ExternalArtifactReference(BaseModel):
    """Pointer to an artifact owned by an external tool."""

    model_config = ConfigDict(frozen=True)

    kind: ExternalArtifactKind = ExternalArtifactKind.ENTITY
    source_system: str
    uri: str
    content_hash: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_system", "uri", "content_hash")
    @classmethod
    def _require_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("External artifact reference fields must not be blank.")
        return cleaned

    @field_validator("metadata")
    @classmethod
    def _normalize_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _normalize_metadata_mapping(value)


def _normalize_json_value(value: Any) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("External artifact metadata floats must be finite.")
        return value
    if isinstance(value, list):
        return [_normalize_json_value(item) for item in value]
    if isinstance(value, dict):
        return _normalize_metadata_mapping(value)
    raise ValueError("External artifact metadata values must be JSON-compatible.")


def _normalize_metadata_mapping(value: Mapping[object, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, metadata_value in value.items():
        cleaned_key = str(key).strip()
        if not cleaned_key:
            raise ValueError("External artifact metadata keys must not be blank.")
        cleaned[cleaned_key] = _normalize_json_value(metadata_value)
    return cleaned


def encode_external_artifacts(
    artifacts: Iterable[ExternalArtifactReference],
) -> str:
    payload = [
        artifact.model_dump(mode="json", exclude_none=True)
        for artifact in artifacts
    ]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def external_artifacts_from_metadata(
    metadata: Mapping[str, str] | None,
) -> list[ExternalArtifactReference]:
    if not metadata:
        return []
    encoded = metadata.get(EXTERNAL_ARTIFACTS_METADATA_KEY)
    if not encoded:
        return []
    decoded = json.loads(encoded)
    if not isinstance(decoded, list):
        raise ValueError("external_artifacts metadata must decode to a list.")
    return [ExternalArtifactReference.model_validate(item) for item in decoded]


def dataset_manifest_from_external_artifact(
    artifact: ExternalArtifactReference,
    *,
    files: Iterable[DatasetFile],
    metadata: Mapping[str, str] | None = None,
) -> DatasetCommitManifestInput:
    """Build a dataset manifest input from one external manifest/substrate pointer."""

    merged_metadata = {str(key): str(value) for key, value in (metadata or {}).items()}
    merged_metadata[EXTERNAL_ARTIFACTS_METADATA_KEY] = encode_external_artifacts([artifact])
    return DatasetCommitManifestInput(
        files=list(files),
        metadata=merged_metadata,
    )
