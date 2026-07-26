"""Client-side adapter for the atomic evidence-bundle API command."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError as PydanticValidationError

from lab_tracker.errors import ValidationError as DomainValidationError
from lab_tracker.mcp_api_client import (
    LabTrackerAPIError,
    LabTrackerAPIValidationError,
)
from lab_tracker.schemas import Envelope, EvidenceBundleResultRead
from lab_tracker.upload_security import validate_upload_content_type
from lab_tracker_client.transport import (
    MAX_UPLOAD_BYTES,
    UploadTooLargeError,
    preflight_upload_size,
)

JsonObject = dict[str, Any]

_COMPONENT_ID_FIELDS: dict[str, tuple[str, ...]] = {
    "dataset": ("dataset_id",),
    "analysis": ("analysis_id",),
    "claim": ("claim_id",),
    # The public MCP shape accepts both aliases, while the strict server union
    # names the existing-visualization discriminator field ``viz_id``.
    "visualization": ("viz_id", "visualization_id"),
    "source_note": ("note_id", "source_note_id"),
}

_COMPONENT_RESULT_FIELDS: dict[str, tuple[str, str]] = {
    "dataset": ("dataset_id", "datasets"),
    "analysis": ("analysis_id", "analyses"),
    "claim": ("claim_id", "claims"),
    "visualization": ("visualization_id", "visualizations"),
    "source_note": ("source_note_id", "notes"),
}


class EvidenceBundleClient(Protocol):
    def record_evidence_bundle(self, **kwargs: Any) -> JsonObject: ...

    def get_visualization(self, visualization_id: str) -> JsonObject: ...

    def upload_visualization_file(self, **kwargs: Any) -> JsonObject: ...


@dataclass(frozen=True)
class _VisualizationUpload:
    path: Path
    content_type: str
    checksum_sha256: str
    size_bytes: int
    _temporary_directory: tempfile.TemporaryDirectory[str]

    @property
    def intent(self) -> JsonObject:
        return {
            "checksum_sha256": self.checksum_sha256,
            "size_bytes": self.size_bytes,
            "filename": self.path.name,
            "content_type": self.content_type,
        }

    def cleanup(self) -> None:
        self._temporary_directory.cleanup()


def record_evidence_bundle(
    client: EvidenceBundleClient,
    *,
    project_id: str,
    primary_question_id: str | None = None,
    dataset: JsonObject | None = None,
    analysis: JsonObject | None = None,
    claim: JsonObject | None = None,
    visualization: JsonObject | None = None,
    source_note: JsonObject | None = None,
    dry_run: bool = True,
    idempotency_key: str | None = None,
) -> JsonObject:
    """Preview or atomically record one evidence bundle through the HTTP API.

    The API owns component validation, transactionality, and idempotency. A local
    visualization file remains on the MCP host: a private immutable snapshot is
    checked and fingerprinted before the bundle request, then that exact snapshot
    is uploaded only after a created/reused response.
    """
    upload = _snapshot_upload(visualization)
    try:
        server_components = {
            "dataset": _component_request("dataset", dataset),
            "analysis": _component_request("analysis", analysis),
            "claim": _component_request("claim", claim),
            "visualization": _component_request("visualization", visualization),
            "source_note": _component_request("source_note", source_note),
        }
        server_visualization = server_components["visualization"]
        if server_visualization is not None and upload is not None:
            server_visualization["upload_intent"] = upload.intent

        response = client.record_evidence_bundle(
            project_id=project_id,
            primary_question_id=primary_question_id,
            dataset=server_components["dataset"],
            analysis=server_components["analysis"],
            claim=server_components["claim"],
            visualization=server_visualization,
            source_note=server_components["source_note"],
            dry_run=dry_run,
            idempotency_key=idempotency_key,
        )
        validated = _validate_bundle_response(
            response,
            requested_components={
                component for component, payload in server_components.items() if payload is not None
            },
        )
        response = _with_legacy_result_buckets(response, validated)
        outcome = validated.outcome

        if upload is None:
            return response
        if dry_run or outcome == "preview":
            return _with_attachment_plan(
                response,
                {
                    "action": "upload",
                    "entity_type": "visualization_asset",
                    "outcome": "preview",
                    "upload_intent": upload.intent,
                },
            )

        visualization_id = _component_id(_bundle_data(response), "visualization_id")
        if visualization_id is None:
            _raise_attachment_failure(
                outcome=outcome,
                idempotency_key=idempotency_key,
                detail="the server response did not include component_ids.visualization_id",
            )

        try:
            existing = client.get_visualization(visualization_id)
            asset = _visualization_asset(existing)
            expected_current_storage_id = "absent"
            if asset is not None:
                expected_current_storage_id = _clean(asset.get("storage_id")) or ""
                if not expected_current_storage_id:
                    raise LabTrackerAPIError(
                        "Visualization response included asset metadata without a storage_id.",
                        code="invalid_visualization_response",
                    )
            uploaded = client.upload_visualization_file(
                viz_id=visualization_id,
                file_path=str(upload.path),
                content_type=upload.content_type,
                checksum_sha256=upload.checksum_sha256,
                size_bytes=upload.size_bytes,
                expected_current_storage_id=expected_current_storage_id,
            )
            uploaded_asset = _validated_uploaded_asset(uploaded, upload)
            asset_outcome = _asset_upload_outcome(uploaded)
            if asset_outcome == "reused":
                return _with_attachment_plan(
                    response,
                    {
                        "action": "reuse",
                        "entity_type": "visualization_asset",
                        "outcome": "reused",
                        "visualization_id": visualization_id,
                        "storage_id": uploaded_asset["storage_id"],
                        "checksum_sha256": upload.checksum_sha256,
                        "reason": "matching_checksum",
                    },
                )
            return _with_attachment_plan(
                response,
                {
                    "action": "uploaded",
                    "entity_type": "visualization_asset",
                    "outcome": "created",
                    "visualization_id": visualization_id,
                    "storage_id": uploaded_asset["storage_id"],
                    "checksum_sha256": upload.checksum_sha256,
                },
            )
        except Exception as exc:
            _raise_attachment_failure(
                outcome=outcome,
                idempotency_key=idempotency_key,
                detail=str(exc),
                cause=exc,
            )
    finally:
        if upload is not None:
            upload.cleanup()


def _component_request(
    component: str,
    payload: JsonObject | None,
) -> JsonObject | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise LabTrackerAPIValidationError(
            f"{component} must be an object.",
            code="validation_error",
        )
    public_payload = dict(payload)
    public_payload.pop("kind", None)
    id_fields = _COMPONENT_ID_FIELDS[component]
    explicit_id = next(
        (
            cleaned
            for field in id_fields
            if (cleaned := _clean(public_payload.get(field))) is not None
        ),
        None,
    )
    for field in id_fields:
        public_payload.pop(field, None)
    if component == "visualization":
        for field in ("upload_file", "upload_file_path", "content_type"):
            public_payload.pop(field, None)
    if explicit_id is not None:
        canonical_id_field = id_fields[0]
        return {"kind": "existing", canonical_id_field: explicit_id}
    return {"kind": "create", **public_payload}


def _snapshot_upload(visualization: JsonObject | None) -> _VisualizationUpload | None:
    if not isinstance(visualization, dict) or visualization.get("upload_file") is not True:
        return None
    file_path = _clean(visualization.get("upload_file_path")) or _clean(
        visualization.get("file_path")
    )
    if file_path is None:
        raise LabTrackerAPIValidationError(
            "Visualization upload requested but no file path was supplied.",
            code="validation_error",
        )
    source_path = Path(file_path).expanduser()
    if not source_path.is_file():
        raise LabTrackerAPIValidationError(
            f"Visualization file does not exist: {file_path}",
            code="validation_error",
        )
    try:
        preflight_upload_size(source_path, max_bytes=MAX_UPLOAD_BYTES)
    except UploadTooLargeError as exc:
        raise LabTrackerAPIValidationError(str(exc), code="validation_error") from exc
    requested_content_type = (
        _clean(visualization.get("content_type"))
        or mimetypes.guess_type(source_path.name)[0]
        or "application/octet-stream"
    )
    try:
        content_type = validate_upload_content_type(requested_content_type)
    except DomainValidationError as exc:
        raise LabTrackerAPIValidationError(str(exc), code="validation_error") from exc

    temporary_directory = tempfile.TemporaryDirectory(
        prefix="lab-tracker-evidence-upload-",
        ignore_cleanup_errors=True,
    )
    snapshot_path = Path(temporary_directory.name) / source_path.name
    try:
        with source_path.open("rb") as source:
            descriptor = os.open(
                snapshot_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as snapshot:
                shutil.copyfileobj(source, snapshot, length=1024 * 1024)
        size_bytes = preflight_upload_size(snapshot_path, max_bytes=MAX_UPLOAD_BYTES)
        if size_bytes <= 0:
            raise LabTrackerAPIValidationError(
                "Visualization file must not be empty.",
                code="validation_error",
            )
        checksum_sha256 = _file_sha256(snapshot_path)
    except (OSError, UploadTooLargeError, LabTrackerAPIValidationError) as exc:
        temporary_directory.cleanup()
        if isinstance(exc, LabTrackerAPIValidationError):
            raise
        raise LabTrackerAPIValidationError(
            f"Could not snapshot visualization upload {file_path}: {exc}",
            code="validation_error",
        ) from exc

    return _VisualizationUpload(
        path=snapshot_path,
        content_type=content_type,
        checksum_sha256=checksum_sha256,
        size_bytes=size_bytes,
        _temporary_directory=temporary_directory,
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_bundle_response(
    response: JsonObject,
    *,
    requested_components: set[str],
) -> EvidenceBundleResultRead:
    try:
        envelope = Envelope[EvidenceBundleResultRead].model_validate(response)
    except PydanticValidationError as exc:
        raise LabTrackerAPIError(
            "Evidence-bundle response did not match the documented response schema.",
            code="invalid_evidence_bundle_response",
        ) from exc

    result = envelope.data
    if result.outcome != "preview":
        missing_ids = [
            result_field
            for component, (result_field, _bucket) in _COMPONENT_RESULT_FIELDS.items()
            if component in requested_components
            and getattr(result.component_ids, result_field) is None
        ]
        if missing_ids:
            raise LabTrackerAPIError(
                "Evidence-bundle response omitted required component IDs: "
                + ", ".join(missing_ids)
                + ".",
                code="invalid_evidence_bundle_response",
            )
    return result


def _with_legacy_result_buckets(
    response: JsonObject,
    result: EvidenceBundleResultRead,
) -> JsonObject:
    created: dict[str, list[str]] = {
        "notes": [],
        "datasets": [],
        "analyses": [],
        "claims": [],
        "visualizations": [],
    }
    reused: dict[str, list[str]] = {
        "notes": [],
        "datasets": [],
        "analyses": [],
        "claims": [],
        "visualizations": [],
    }
    for step in result.plan:
        result_field, bucket = _COMPONENT_RESULT_FIELDS[step.entity_type]
        entity_id = step.entity_id or getattr(result.component_ids, result_field)
        if entity_id is None:
            continue
        entity_id_text = str(entity_id)
        if step.action == "reuse":
            if entity_id_text not in reused[bucket]:
                reused[bucket].append(entity_id_text)
        elif result.outcome != "preview" and entity_id_text not in created[bucket]:
            created[bucket].append(entity_id_text)

    enriched = dict(response)
    data = dict(_bundle_data(response))
    data["created"] = created
    data["reused"] = reused
    enriched["data"] = data
    return enriched


def _bundle_data(response: JsonObject) -> JsonObject:
    data = response.get("data")
    if not isinstance(data, dict):
        raise LabTrackerAPIError(
            "Evidence-bundle response did not include object data.",
            code="invalid_evidence_bundle_response",
        )
    return data


def _component_id(data: JsonObject, field: str) -> str | None:
    component_ids = data.get("component_ids")
    if not isinstance(component_ids, dict):
        return None
    return _clean(component_ids.get(field))


def _visualization_asset(response: JsonObject) -> JsonObject | None:
    data = response.get("data")
    if not isinstance(data, dict):
        raise LabTrackerAPIError(
            "Visualization response did not include object data.",
            code="invalid_visualization_response",
        )
    asset = data.get("asset")
    return dict(asset) if isinstance(asset, dict) else None


def _validated_uploaded_asset(
    response: JsonObject,
    upload: _VisualizationUpload,
) -> JsonObject:
    asset = _visualization_asset(response)
    if asset is None:
        raise LabTrackerAPIError(
            "Visualization upload response did not include asset metadata.",
            code="invalid_visualization_response",
        )

    mismatches: list[str] = []
    if _clean(asset.get("storage_id")) is None:
        mismatches.append("storage_id")
    if _clean(asset.get("filename")) != upload.path.name:
        mismatches.append("filename")
    if (_clean(asset.get("content_type")) or "").casefold() != upload.content_type.casefold():
        mismatches.append("content_type")
    size_bytes = asset.get("size_bytes")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes != upload.size_bytes
    ):
        mismatches.append("size_bytes")
    if not _same_checksum(asset.get("checksum"), upload.checksum_sha256):
        mismatches.append("checksum")
    if mismatches:
        raise LabTrackerAPIError(
            "Visualization upload response did not match the fingerprinted snapshot "
            f"({', '.join(dict.fromkeys(mismatches))}).",
            code="invalid_visualization_response",
        )
    return asset


def _asset_upload_outcome(response: JsonObject) -> str:
    meta = response.get("meta")
    if meta is None:
        # Backward-compatible with servers predating atomic attachment outcomes.
        return "created"
    if not isinstance(meta, dict):
        raise LabTrackerAPIError(
            "Visualization upload response included malformed metadata.",
            code="invalid_visualization_response",
        )
    outcome = meta.get("asset_outcome")
    if outcome is None:
        return "created"
    if outcome not in {"created", "replaced", "reused"}:
        raise LabTrackerAPIError(
            "Visualization upload response included an unknown asset outcome.",
            code="invalid_visualization_response",
        )
    return outcome


def _same_checksum(value: object, expected: str) -> bool:
    actual = _clean(value)
    if actual is None:
        return False
    return actual.removeprefix("sha256:").casefold() == expected.casefold()


def _with_attachment_plan(response: JsonObject, entry: JsonObject) -> JsonObject:
    result = dict(response)
    data = dict(_bundle_data(response))
    plan = data.get("plan")
    if not isinstance(plan, list):
        raise LabTrackerAPIError(
            "Evidence-bundle response did not include a reviewable plan.",
            code="invalid_evidence_bundle_response",
        )
    data["plan"] = [*plan, entry]
    data["attachment"] = entry
    result["data"] = data
    return result


def _raise_attachment_failure(
    *,
    outcome: str,
    idempotency_key: str | None,
    detail: str,
    cause: Exception | None = None,
) -> None:
    retry = (
        f" Retry with the same idempotency_key ({idempotency_key!r}); the bundle "
        "endpoint will reuse the committed graph records."
        if idempotency_key
        else " The graph records were not rolled back; inspect them before retrying the upload."
    )
    error = LabTrackerAPIError(
        "Evidence bundle was "
        f"{outcome}, but its visualization attachment failed: {detail}. Server graph "
        f"records are already committed and were not rolled back.{retry}",
        code="evidence_bundle_attachment_failed",
    )
    if cause is None:
        raise error
    raise error from cause


def _clean(value: object) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None
