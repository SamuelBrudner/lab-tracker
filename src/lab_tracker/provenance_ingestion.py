"""PROV-O ingestion helpers for external artifacts."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

from lab_tracker.models import (
    DatasetCommitManifestInput,
    DatasetFile,
    ExternalArtifactKind,
    ExternalArtifactReference,
)

EXTERNAL_ARTIFACTS_METADATA_KEY = "external_artifacts"

__all__ = [
    "EXTERNAL_ARTIFACTS_METADATA_KEY",
    "ExternalArtifactKind",
    "ExternalArtifactReference",
    "dataset_manifest_from_external_artifact",
    "encode_external_artifacts",
    "external_artifacts_from_metadata",
]


def encode_external_artifacts(
    artifacts: Iterable[ExternalArtifactReference],
) -> str:
    payload = [
        artifact.model_dump(mode="json", exclude_none=True)
        for artifact in artifacts
    ]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _external_artifact_identity(artifact: ExternalArtifactReference) -> str:
    return json.dumps(
        artifact.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _deduplicate_exact_external_artifacts(
    artifacts: Iterable[ExternalArtifactReference],
) -> list[ExternalArtifactReference]:
    deduplicated: list[ExternalArtifactReference] = []
    seen: set[str] = set()
    for artifact in artifacts:
        key = _external_artifact_identity(artifact)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(artifact)
    return deduplicated


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
    return _deduplicate_exact_external_artifacts(
        ExternalArtifactReference.model_validate(item) for item in decoded
    )


def dataset_manifest_from_external_artifact(
    artifact: ExternalArtifactReference,
    *,
    files: Iterable[DatasetFile],
    metadata: Mapping[str, str] | None = None,
) -> DatasetCommitManifestInput:
    """Build a dataset manifest input from one external manifest/substrate pointer."""

    merged_metadata = {str(key): str(value) for key, value in (metadata or {}).items()}
    return DatasetCommitManifestInput(
        files=list(files),
        external_artifacts=[artifact],
        metadata=merged_metadata,
    )
