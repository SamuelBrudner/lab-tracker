"""Shared helpers and constants for domain services."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import TYPE_CHECKING, TypeVar
from uuid import UUID

from lab_tracker.auth import LOCAL_AUTH_USER_ID, AuthContext, Role
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import (
    AcquisitionOutput,
    Analysis,
    AnalysisStatus,
    ClaimStatus,
    Dataset,
    DatasetCommitManifest,
    DatasetCommitManifestInput,
    DatasetFile,
    DatasetStatus,
    ExternalArtifactReference,
    Note,
    NoteMetadataScalar,
    Question,
    QuestionLink,
    QuestionLinkRole,
    QuestionStatus,
    SessionStatus,
)
from lab_tracker.provenance_ingestion import external_artifacts_from_metadata

if TYPE_CHECKING:
    from lab_tracker.repository import LabTrackerRepository

WRITE_ROLES = {Role.ADMIN, Role.EDITOR}
StatusT = TypeVar("StatusT", bound=Enum)


def actor_user_id(actor: AuthContext | None) -> str | None:
    if actor is None:
        return None
    return str(actor.user_id)


def actor_user_fk(
    actor: AuthContext | None,
    repository: LabTrackerRepository,
) -> UUID | None:
    if actor is None or actor.user_id == LOCAL_AUTH_USER_ID:
        return None
    user_exists = getattr(repository, "user_exists", None)
    if not callable(user_exists) or not user_exists(actor.user_id):
        return None
    return actor.user_id


def ensure_non_empty(value: str, field_name: str) -> None:
    if not value or not str(value).strip():
        raise ValidationError(f"{field_name} must not be empty.")


def _is_note_metadata_scalar(value: object) -> bool:
    return isinstance(value, (str, bool, int, float))


def normalize_note_metadata(metadata: dict[str, NoteMetadataScalar] | None) -> dict[str, str]:
    if not metadata:
        return {}
    cleaned: dict[str, str] = {}
    for key, value in metadata.items():
        ensure_non_empty(key, "metadata key")
        if not _is_note_metadata_scalar(value):
            raise ValidationError("metadata values must be strings, numbers, or booleans.")
        cleaned_key = str(key).strip()
        cleaned_value = value.strip() if isinstance(value, str) else str(value)
        cleaned[cleaned_key] = cleaned_value
    return cleaned


def _normalized_query(query: str) -> str:
    return (query or "").casefold().strip()


def question_matches_substring(question: Question, query: str) -> bool:
    needle = _normalized_query(query)
    if not needle:
        return False
    hypothesis = question.hypothesis
    return needle in question.text.casefold() or (
        hypothesis is not None and needle in hypothesis.casefold()
    )


def note_matches_substring(note: Note, query: str) -> bool:
    needle = _normalized_query(query)
    if not needle:
        return False
    if needle in note.raw_content.casefold():
        return True
    return note.transcribed_text is not None and needle in note.transcribed_text.casefold()


def _get_or_raise(store: dict[UUID, object], entity_id: UUID, label: str):
    try:
        return store[entity_id]
    except KeyError as exc:
        raise NotFoundError(f"{label} does not exist.") from exc


def unique_ids(values: Iterable[UUID] | None) -> list[UUID]:
    if not values:
        return []
    seen: set[UUID] = set()
    unique: list[UUID] = []
    for value in values:
        if value in seen:
            raise ValidationError("Duplicate id in list.")
        seen.add(value)
        unique.append(value)
    return unique


_QUESTION_STATUS_TRANSITIONS: dict[QuestionStatus, set[QuestionStatus]] = {
    QuestionStatus.STAGED: {
        QuestionStatus.STAGED,
        QuestionStatus.ACTIVE,
        QuestionStatus.ABANDONED,
        QuestionStatus.SUPERSEDED,
    },
    QuestionStatus.ACTIVE: {
        QuestionStatus.ACTIVE,
        QuestionStatus.ANSWERED,
        QuestionStatus.ABANDONED,
        QuestionStatus.SUPERSEDED,
    },
    QuestionStatus.ANSWERED: {QuestionStatus.ANSWERED},
    QuestionStatus.ABANDONED: {QuestionStatus.ABANDONED},
    QuestionStatus.SUPERSEDED: {QuestionStatus.SUPERSEDED},
}

_ANALYSIS_STATUS_TRANSITIONS: dict[AnalysisStatus, set[AnalysisStatus]] = {
    AnalysisStatus.STAGED: {
        AnalysisStatus.STAGED,
        AnalysisStatus.COMMITTED,
        AnalysisStatus.ARCHIVED,
    },
    AnalysisStatus.COMMITTED: {AnalysisStatus.COMMITTED, AnalysisStatus.ARCHIVED},
    AnalysisStatus.ARCHIVED: {AnalysisStatus.ARCHIVED},
}

_DATASET_STATUS_TRANSITIONS: dict[DatasetStatus, set[DatasetStatus]] = {
    DatasetStatus.STAGED: {
        DatasetStatus.STAGED,
        DatasetStatus.COMMITTED,
        DatasetStatus.ARCHIVED,
    },
    DatasetStatus.COMMITTED: {DatasetStatus.COMMITTED, DatasetStatus.ARCHIVED},
    DatasetStatus.ARCHIVED: {DatasetStatus.ARCHIVED},
}

_SESSION_STATUS_TRANSITIONS: dict[SessionStatus, set[SessionStatus]] = {
    SessionStatus.ACTIVE: {SessionStatus.ACTIVE, SessionStatus.CLOSED},
    SessionStatus.CLOSED: {SessionStatus.CLOSED},
}

_CLAIM_STATUS_TRANSITIONS: dict[ClaimStatus, set[ClaimStatus]] = {
    ClaimStatus.PROPOSED: {ClaimStatus.PROPOSED, ClaimStatus.SUPPORTED, ClaimStatus.REJECTED},
    ClaimStatus.SUPPORTED: {ClaimStatus.SUPPORTED},
    ClaimStatus.REJECTED: {ClaimStatus.REJECTED},
}


def _ensure_status_transition(
    current_status: StatusT,
    next_status: StatusT,
    transitions: Mapping[StatusT, set[StatusT]],
    *,
    entity_name: str,
) -> None:
    allowed = transitions.get(current_status, {current_status})
    if next_status not in allowed:
        raise ValidationError(
            f"{entity_name} status cannot transition "
            f"from {current_status.value} to {next_status.value}."
        )


def _ensure_question_status_transition(
    current_status: QuestionStatus,
    next_status: QuestionStatus,
) -> None:
    _ensure_status_transition(
        current_status,
        next_status,
        _QUESTION_STATUS_TRANSITIONS,
        entity_name="Question",
    )


def _ensure_analysis_status_transition(
    current_status: AnalysisStatus,
    next_status: AnalysisStatus,
) -> None:
    _ensure_status_transition(
        current_status,
        next_status,
        _ANALYSIS_STATUS_TRANSITIONS,
        entity_name="Analysis",
    )


def _ensure_dataset_status_transition(
    current_status: DatasetStatus,
    next_status: DatasetStatus,
) -> None:
    _ensure_status_transition(
        current_status,
        next_status,
        _DATASET_STATUS_TRANSITIONS,
        entity_name="Dataset",
    )


def _ensure_session_status_transition(
    current_status: SessionStatus,
    next_status: SessionStatus,
) -> None:
    _ensure_status_transition(
        current_status,
        next_status,
        _SESSION_STATUS_TRANSITIONS,
        entity_name="Session",
    )


def _ensure_claim_status_transition(
    current_status: ClaimStatus,
    next_status: ClaimStatus,
) -> None:
    _ensure_status_transition(
        current_status,
        next_status,
        _CLAIM_STATUS_TRANSITIONS,
        entity_name="Claim",
    )


def _ensure_question_parents_dag(
    question_id: UUID,
    parent_ids: list[UUID],
    store: dict[UUID, Question],
) -> None:
    if question_id in parent_ids:
        raise ValidationError("Question cannot be its own parent.")
    for parent_id in parent_ids:
        if _is_question_ancestor(parent_id, question_id, store):
            raise ValidationError("Question parent graph must be acyclic.")


def _is_question_ancestor(
    start_id: UUID,
    target_id: UUID,
    store: dict[UUID, Question],
) -> bool:
    stack = [start_id]
    visited: set[UUID] = set()
    while stack:
        current = stack.pop()
        if current == target_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        question = store.get(current)
        if question is None:
            continue
        stack.extend(question.parent_question_ids)
    return False


def _ensure_primary_question_active(question: Question) -> None:
    if question.status != QuestionStatus.ACTIVE:
        raise ValidationError("Primary question must be active to commit a dataset.")


def _ensure_claim_confidence(confidence: float) -> None:
    if confidence < 0 or confidence > 100:
        raise ValidationError("confidence must be between 0 and 100.")


def _ensure_claim_support_links(
    status: ClaimStatus,
    dataset_ids: list[UUID],
    analysis_ids: list[UUID],
) -> None:
    if status == ClaimStatus.SUPPORTED and not (dataset_ids or analysis_ids):
        raise ValidationError("Supported claims require supporting datasets or analyses.")


def _analysis_has_question_link(
    analysis: Analysis,
    question_id: UUID,
    datasets: dict[UUID, Dataset],
) -> bool:
    for dataset_id in analysis.dataset_ids:
        dataset = datasets.get(dataset_id)
        if dataset is None:
            continue
        if any(link.question_id == question_id for link in dataset.question_links):
            return True
    return False


def _normalize_dataset_files(files: Iterable[DatasetFile]) -> list[DatasetFile]:
    normalized: list[DatasetFile] = []
    seen: set[str] = set()
    for file in files:
        ensure_non_empty(file.path, "file.path")
        ensure_non_empty(file.checksum, "file.checksum")
        path = file.path.strip()
        checksum = file.checksum.strip()
        if path in seen:
            raise ValidationError("Duplicate file path in commit manifest.")
        seen.add(path)
        normalized.append(DatasetFile(path=path, checksum=checksum))
    return normalized


def _find_acquisition_output(
    outputs: dict[UUID, AcquisitionOutput],
    session_id: UUID,
    file_path: str,
) -> AcquisitionOutput | None:
    for output in outputs.values():
        if output.session_id == session_id and output.file_path == file_path:
            return output
    return None


def _merge_acquisition_outputs(
    manifest: DatasetCommitManifestInput | DatasetCommitManifest | None,
    outputs: Iterable[AcquisitionOutput],
) -> DatasetCommitManifestInput | DatasetCommitManifest | None:
    outputs_list = list(outputs)
    if not outputs_list:
        return manifest
    if isinstance(manifest, DatasetCommitManifest):
        manifest_input = _manifest_input_from_commit(manifest)
    else:
        manifest_input = manifest or DatasetCommitManifestInput()
    merged_files = list(manifest_input.files)
    seen = {file.path.strip(): file.checksum.strip() for file in manifest_input.files}
    for output in outputs_list:
        path = output.file_path.strip()
        checksum = output.checksum.strip()
        existing = seen.get(path)
        if existing is None:
            merged_files.append(DatasetFile(path=path, checksum=checksum))
            seen[path] = checksum
            continue
        if existing != checksum:
            raise ValidationError("Acquisition output checksum conflict for file path.")
    return DatasetCommitManifestInput(
        files=merged_files,
        external_artifacts=manifest_input.external_artifacts,
        metadata=manifest_input.metadata,
        nwb_metadata=manifest_input.nwb_metadata,
        bids_metadata=manifest_input.bids_metadata,
        note_ids=manifest_input.note_ids,
        source_session_id=manifest_input.source_session_id,
    )


def _normalize_commit_metadata(metadata: dict[str, str] | None) -> dict[str, str]:
    if not metadata:
        return {}
    cleaned: dict[str, str] = {}
    for key, value in metadata.items():
        ensure_non_empty(key, "metadata key")
        cleaned_key = str(key).strip()
        cleaned_value = value.strip() if isinstance(value, str) else str(value)
        cleaned[cleaned_key] = cleaned_value
    return cleaned


def _legacy_external_artifacts(metadata: dict[str, str]) -> list[ExternalArtifactReference]:
    try:
        return external_artifacts_from_metadata(metadata)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _normalize_external_artifacts(
    artifacts: Iterable[ExternalArtifactReference],
) -> list[ExternalArtifactReference]:
    normalized: list[ExternalArtifactReference] = []
    seen: set[tuple[str, str, str]] = set()
    for artifact in artifacts:
        item = ExternalArtifactReference.model_validate(artifact)
        key = _external_artifact_key(item)
        if key in seen:
            raise ValidationError("Duplicate external artifact reference in commit manifest.")
        seen.add(key)
        normalized.append(item)
    return normalized


def _merge_external_artifacts(
    artifacts: Iterable[ExternalArtifactReference],
    legacy_artifacts: Iterable[ExternalArtifactReference],
) -> list[ExternalArtifactReference]:
    merged: list[ExternalArtifactReference] = []
    seen: dict[tuple[str, str, str], ExternalArtifactReference] = {}
    for artifact in [*legacy_artifacts, *artifacts]:
        item = ExternalArtifactReference.model_validate(artifact)
        key = _external_artifact_key(item)
        if key in seen:
            if seen[key] != item:
                raise ValidationError("Conflicting external artifact reference in commit manifest.")
            continue
        seen[key] = item
        merged.append(item)
    return _normalize_external_artifacts(merged)


def _external_artifact_key(artifact: ExternalArtifactReference) -> tuple[str, str, str]:
    return (artifact.kind.value, artifact.source_system, artifact.uri)


def _manifest_input_from_commit(manifest: DatasetCommitManifest) -> DatasetCommitManifestInput:
    return DatasetCommitManifestInput(
        files=list(manifest.files),
        external_artifacts=list(manifest.external_artifacts),
        metadata=dict(manifest.metadata),
        nwb_metadata=dict(manifest.nwb_metadata),
        bids_metadata=dict(manifest.bids_metadata),
        note_ids=list(manifest.note_ids),
        source_session_id=manifest.source_session_id,
    )


def _manifest_input_with_source(
    manifest: DatasetCommitManifestInput | DatasetCommitManifest | None,
    source_session_id: UUID,
) -> DatasetCommitManifestInput:
    if isinstance(manifest, DatasetCommitManifest):
        manifest_input = _manifest_input_from_commit(manifest)
    else:
        manifest_input = manifest
    if manifest_input is None:
        return DatasetCommitManifestInput(source_session_id=source_session_id)
    if (
        manifest_input.source_session_id is not None
        and manifest_input.source_session_id != source_session_id
    ):
        raise ValidationError("commit_manifest source_session_id does not match session.")
    return DatasetCommitManifestInput(
        files=manifest_input.files,
        external_artifacts=manifest_input.external_artifacts,
        metadata=manifest_input.metadata,
        nwb_metadata=manifest_input.nwb_metadata,
        bids_metadata=manifest_input.bids_metadata,
        note_ids=manifest_input.note_ids,
        source_session_id=source_session_id,
    )


def _build_commit_manifest(
    manifest: DatasetCommitManifestInput | DatasetCommitManifest | None,
    question_links: list[QuestionLink],
) -> DatasetCommitManifest:
    if isinstance(manifest, DatasetCommitManifest):
        manifest_input = _manifest_input_from_commit(manifest)
    else:
        manifest_input = manifest or DatasetCommitManifestInput()
    base_metadata = _normalize_commit_metadata(manifest_input.metadata)
    nwb_metadata = _normalize_commit_metadata(manifest_input.nwb_metadata)
    bids_metadata = _normalize_commit_metadata(manifest_input.bids_metadata)
    legacy_external_artifacts = _legacy_external_artifacts(base_metadata)
    explicit_external_artifacts = _normalize_external_artifacts(
        manifest_input.external_artifacts
    )
    external_artifacts = _merge_external_artifacts(
        explicit_external_artifacts,
        legacy_external_artifacts,
    )
    return DatasetCommitManifest(
        files=_normalize_dataset_files(manifest_input.files),
        external_artifacts=external_artifacts,
        metadata=base_metadata,
        nwb_metadata=nwb_metadata,
        bids_metadata=bids_metadata,
        note_ids=unique_ids(manifest_input.note_ids),
        question_links=list(question_links),
        source_session_id=manifest_input.source_session_id,
    )


def _validate_commit_hash(provided: str | None, expected: str) -> None:
    if provided is None:
        return
    ensure_non_empty(provided, "commit_hash")
    if provided.strip() != expected:
        raise ValidationError("commit_hash must match content-addressed manifest hash.")


_ROLE_ORDER = {QuestionLinkRole.PRIMARY.value: 0, QuestionLinkRole.SECONDARY.value: 1}


def _manifest_payload(manifest: DatasetCommitManifest) -> dict[str, object]:
    files = sorted(
        ({"path": file.path, "checksum": file.checksum} for file in manifest.files),
        key=lambda item: (item["path"], item["checksum"]),
    )
    external_artifacts = _external_artifacts_payload(manifest.external_artifacts)
    links = sorted(
        (
            {
                "question_id": str(link.question_id),
                "role": link.role.value,
                "outcome_status": link.outcome_status.value,
            }
            for link in manifest.question_links
        ),
        key=lambda item: (_ROLE_ORDER.get(item["role"], 99), item["question_id"]),
    )
    note_ids = sorted(str(note_id) for note_id in manifest.note_ids)
    metadata = {key: manifest.metadata[key] for key in sorted(manifest.metadata)}
    nwb_metadata = {key: manifest.nwb_metadata[key] for key in sorted(manifest.nwb_metadata)}
    bids_metadata = {key: manifest.bids_metadata[key] for key in sorted(manifest.bids_metadata)}
    payload: dict[str, object] = {
        "files": files,
        "metadata": metadata,
        "nwb_metadata": nwb_metadata,
        "bids_metadata": bids_metadata,
        "question_links": links,
        "note_ids": note_ids,
        "source_session_id": str(manifest.source_session_id)
        if manifest.source_session_id
        else None,
    }
    legacy_external_artifacts = _external_artifacts_payload(
        _legacy_external_artifacts(manifest.metadata)
    )
    if external_artifacts and external_artifacts != legacy_external_artifacts:
        payload["external_artifacts"] = external_artifacts
    return payload


def _external_artifacts_payload(
    artifacts: Iterable[ExternalArtifactReference],
) -> list[dict[str, object]]:
    return sorted(
        (
            artifact.model_dump(mode="json", exclude_none=True)
            for artifact in artifacts
        ),
        key=lambda item: (
            str(item.get("kind", "")),
            str(item.get("source_system", "")),
            str(item.get("uri", "")),
            str(item.get("content_hash", "")),
        ),
    )


def _compute_commit_hash(manifest: DatasetCommitManifest) -> str:
    payload = _manifest_payload(manifest)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
