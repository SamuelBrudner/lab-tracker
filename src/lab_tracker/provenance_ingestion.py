"""PROV-O ingestion helpers for external artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote

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
    "dataset_manifest_from_datalad_manifest",
    "dataset_manifest_from_dvc_manifest",
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


def dataset_manifest_from_dvc_manifest(
    manifest: Mapping[str, Any] | str,
    *,
    manifest_uri: str,
    content_hash: str | None = None,
    metadata: Mapping[str, str] | None = None,
    nwb_metadata: Mapping[str, str] | None = None,
    bids_metadata: Mapping[str, str] | None = None,
) -> DatasetCommitManifestInput:
    """Build a dataset manifest input from a DVC .dvc manifest/pointer file."""

    parsed = _coerce_manifest_mapping(manifest)
    outs = _mapping_list(parsed.get("outs"), "DVC manifest outs")
    files = [
        _dataset_file_from_mapping(out, source_system="dvc")
        for out in outs
        if _text_value(out, "path")
    ]
    artifact = ExternalArtifactReference(
        source_system="dvc",
        uri=manifest_uri,
        content_hash=content_hash or _manifest_content_hash(parsed),
        metadata={
            "out_count": len(outs),
            "paths": [file.path for file in files],
            "native": parsed,
        },
    )
    return DatasetCommitManifestInput(
        files=files,
        external_artifacts=[artifact],
        metadata=_string_metadata(metadata),
        nwb_metadata=_string_metadata(
            _merged_manifest_metadata(parsed, "nwb_metadata", nwb_metadata)
        ),
        bids_metadata=_string_metadata(
            _merged_manifest_metadata(parsed, "bids_metadata", bids_metadata)
        ),
    )


def dataset_manifest_from_datalad_manifest(
    manifest: Mapping[str, Any] | Iterable[Mapping[str, Any]] | str,
    *,
    dataset_uri: str | None = None,
    commit: str | None = None,
    artifact_uri: str | None = None,
    content_hash: str | None = None,
    metadata: Mapping[str, str] | None = None,
    nwb_metadata: Mapping[str, str] | None = None,
    bids_metadata: Mapping[str, str] | None = None,
) -> DatasetCommitManifestInput:
    """Build a dataset manifest input from exported DataLad status/file records."""

    parsed = _coerce_datalad_manifest(manifest)
    records = _datalad_records(parsed)
    files = [
        _dataset_file_from_mapping(record, source_system="datalad")
        for record in records
        if _text_value(record, "path")
    ]
    resolved_dataset_uri = (
        dataset_uri
        or _text_value(parsed, "dataset_uri")
        or _text_value(parsed, "dataset")
        or _text_value(parsed, "path")
    )
    resolved_commit = commit or _text_value(parsed, "commit") or _text_value(parsed, "revision")
    resolved_artifact_uri = artifact_uri or _text_value(parsed, "uri")
    if not resolved_artifact_uri:
        if not resolved_dataset_uri:
            raise ValueError("DataLad manifest import requires a dataset URI or artifact URI.")
        resolved_artifact_uri = _datalad_artifact_uri(resolved_dataset_uri, resolved_commit)
    artifact = ExternalArtifactReference(
        source_system="datalad",
        uri=resolved_artifact_uri,
        content_hash=content_hash or _manifest_content_hash(parsed),
        metadata={
            "commit": resolved_commit,
            "file_count": len(files),
            "paths": [file.path for file in files],
            "native": parsed,
        },
    )
    return DatasetCommitManifestInput(
        files=files,
        external_artifacts=[artifact],
        metadata=_string_metadata(metadata),
        nwb_metadata=_string_metadata(
            _merged_manifest_metadata(parsed, "nwb_metadata", nwb_metadata)
        ),
        bids_metadata=_string_metadata(
            _merged_manifest_metadata(parsed, "bids_metadata", bids_metadata)
        ),
    )


def _coerce_manifest_mapping(manifest: Mapping[str, Any] | str) -> dict[str, Any]:
    if isinstance(manifest, str):
        try:
            decoded = json.loads(manifest)
        except json.JSONDecodeError:
            decoded = _parse_simple_yaml_mapping(manifest)
    else:
        decoded = dict(manifest)
    if not isinstance(decoded, dict):
        raise ValueError("Manifest must decode to an object.")
    return decoded


def _coerce_datalad_manifest(
    manifest: Mapping[str, Any] | Iterable[Mapping[str, Any]] | str,
) -> dict[str, Any]:
    if isinstance(manifest, str):
        stripped = manifest.strip()
        if "\n" in stripped and not stripped.startswith(("{", "[")):
            records = [json.loads(line) for line in stripped.splitlines() if line.strip()]
            return {"files": records}
        decoded = json.loads(stripped)
        if isinstance(decoded, list):
            return {"files": decoded}
        if isinstance(decoded, dict):
            return decoded
        raise ValueError("DataLad manifest must decode to an object or list.")
    if isinstance(manifest, Mapping):
        return dict(manifest)
    return {"files": [dict(item) for item in manifest]}


def _datalad_records(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_records = manifest.get("files") or manifest.get("records") or manifest.get("items")
    if raw_records is None:
        return [manifest]
    return _mapping_list(raw_records, "DataLad manifest files")


def _mapping_list(value: object, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    result: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"{label} entries must be objects.")
        result.append(item)
    return result


def _dataset_file_from_mapping(item: Mapping[str, Any], *, source_system: str) -> DatasetFile:
    path = _required_text_value(item, "path")
    checksum = _file_checksum(item, source_system=source_system)
    return DatasetFile(
        path=path,
        checksum=checksum,
        size_bytes=_optional_int_value(item, "size_bytes", "size", "bytesize"),
    )


def _file_checksum(item: Mapping[str, Any], *, source_system: str) -> str:
    explicit = _text_value(item, "checksum")
    if explicit:
        return explicit
    if source_system == "dvc":
        md5 = _text_value(item, "md5")
        if md5:
            return f"md5:{md5}"
    annex_key = _text_value(item, "key") or _text_value(item, "annexkey")
    if annex_key:
        return f"datalad-key:{annex_key}"
    digest = _text_value(item, "hash") or _text_value(item, "gitshasum")
    if digest:
        algorithm = _text_value(item, "hash_type") or source_system
        return f"{algorithm}:{digest}"
    raise ValueError(f"{source_system} manifest file is missing a checksum.")


def _text_value(item: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        cleaned = str(value).strip()
        if cleaned:
            return cleaned
    return None


def _required_text_value(item: Mapping[str, Any], key: str) -> str:
    value = _text_value(item, key)
    if value is None:
        raise ValueError(f"Manifest file entry is missing {key}.")
    return value


def _optional_int_value(item: Mapping[str, Any], *keys: str) -> int | None:
    value = _text_value(item, *keys)
    if value is None:
        return None
    return int(value)


def _string_metadata(metadata: Mapping[str, str] | None) -> dict[str, str]:
    return {str(key): str(value) for key, value in (metadata or {}).items()}


def _merged_manifest_metadata(
    manifest: Mapping[str, Any],
    key: str,
    provided: Mapping[str, str] | None,
) -> dict[str, str]:
    merged = _string_metadata(_mapping_value(manifest.get(key), key))
    merged.update(_string_metadata(provided))
    return merged


def _mapping_value(value: object, label: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object when provided.")
    return value


def _datalad_artifact_uri(dataset_uri: str, commit: str | None) -> str:
    if "://" not in dataset_uri:
        dataset_uri = f"datalad://{dataset_uri.strip('/')}"
    if not commit:
        return dataset_uri
    separator = "&" if "?" in dataset_uri else "?"
    return f"{dataset_uri}{separator}commit={quote(commit, safe='')}"


def _manifest_content_hash(manifest: object) -> str:
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _parse_simple_yaml_mapping(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_list: list[dict[str, Any]] | None = None
    current_item: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if current_list is not None and stripped.startswith("- "):
            current_item = {}
            current_list.append(current_item)
            remainder = stripped[2:].strip()
            if remainder:
                key, value = _split_simple_yaml_pair(remainder)
                if value is None:
                    raise ValueError("Inline list entries must use key/value pairs.")
                current_item[key] = _parse_simple_scalar(value)
            continue
        if not raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            key, value = _split_simple_yaml_pair(stripped)
            if value is None:
                current_list = []
                result[key] = current_list
                current_item = None
            else:
                result[key] = _parse_simple_scalar(value)
                current_list = None
                current_item = None
            continue
        if current_list is None:
            raise ValueError("Only simple top-level lists are supported in manifest YAML.")
        if current_item is None:
            raise ValueError("Nested YAML values must belong to a list item.")
        key, value = _split_simple_yaml_pair(stripped)
        if value is None:
            raise ValueError("Nested manifest mappings are not supported.")
        current_item[key] = _parse_simple_scalar(value)
    return result


def _split_simple_yaml_pair(line: str) -> tuple[str, str | None]:
    if ":" not in line:
        raise ValueError("Manifest YAML lines must use key/value pairs.")
    key, value = line.split(":", 1)
    cleaned_key = key.strip()
    if not cleaned_key:
        raise ValueError("Manifest YAML keys must not be blank.")
    cleaned_value = value.strip()
    return cleaned_key, cleaned_value or None


def _parse_simple_scalar(value: str) -> str | int | bool | None:
    cleaned = value.strip().strip("'\"")
    lowered = cleaned.lower()
    if lowered in {"null", "none", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if cleaned.isdigit():
        return int(cleaned)
    return cleaned
