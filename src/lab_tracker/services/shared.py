"""Shared helpers and constants for domain services."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Iterable
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, Role
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
    EntityTagSuggestion,
    ExtractedEntity,
    Note,
    Question,
    QuestionLink,
    QuestionLinkRole,
    QuestionStatus,
    SessionStatus,
)

WRITE_ROLES = {Role.ADMIN, Role.EDITOR}


def _actor_user_id(actor: AuthContext | None) -> str | None:
    if actor is None:
        return None
    return str(actor.user_id)


def _ensure_non_empty(value: str, field_name: str) -> None:
    if not value or not str(value).strip():
        raise ValidationError(f"{field_name} must not be empty.")


def _normalize_note_metadata(metadata: dict[str, str] | None) -> dict[str, str]:
    if not metadata:
        return {}
    cleaned: dict[str, str] = {}
    for key, value in metadata.items():
        _ensure_non_empty(key, "metadata key")
        cleaned_key = str(key).strip()
        cleaned_value = value.strip() if isinstance(value, str) else str(value)
        cleaned[cleaned_key] = cleaned_value
    return cleaned


def _get_or_raise(store: dict[UUID, object], entity_id: UUID, label: str):
    try:
        return store[entity_id]
    except KeyError as exc:
        raise NotFoundError(f"{label} does not exist.") from exc


def _unique_ids(values: Iterable[UUID] | None) -> list[UUID]:
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
    QuestionStatus.STAGED: {QuestionStatus.STAGED, QuestionStatus.ACTIVE, QuestionStatus.ABANDONED},
    QuestionStatus.ACTIVE: {
        QuestionStatus.ACTIVE,
        QuestionStatus.ANSWERED,
        QuestionStatus.ABANDONED,
    },
    QuestionStatus.ANSWERED: {QuestionStatus.ANSWERED},
    QuestionStatus.ABANDONED: {QuestionStatus.ABANDONED},
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


def _ensure_question_status_transition(
    current_status: QuestionStatus,
    next_status: QuestionStatus,
) -> None:
    allowed = _QUESTION_STATUS_TRANSITIONS.get(current_status, {current_status})
    if next_status not in allowed:
        raise ValidationError(
            f"Question status cannot transition from {current_status.value} to {next_status.value}."
        )


def _ensure_analysis_status_transition(
    current_status: AnalysisStatus,
    next_status: AnalysisStatus,
) -> None:
    allowed = _ANALYSIS_STATUS_TRANSITIONS.get(current_status, {current_status})
    if next_status not in allowed:
        raise ValidationError(
            f"Analysis status cannot transition from {current_status.value} to {next_status.value}."
        )


def _ensure_dataset_status_transition(
    current_status: DatasetStatus,
    next_status: DatasetStatus,
) -> None:
    allowed = _DATASET_STATUS_TRANSITIONS.get(current_status, {current_status})
    if next_status not in allowed:
        raise ValidationError(
            f"Dataset status cannot transition from {current_status.value} to {next_status.value}."
        )


def _ensure_session_status_transition(
    current_status: SessionStatus,
    next_status: SessionStatus,
) -> None:
    allowed = _SESSION_STATUS_TRANSITIONS.get(current_status, {current_status})
    if next_status not in allowed:
        raise ValidationError(
            f"Session status cannot transition from {current_status.value} to {next_status.value}."
        )


def _ensure_claim_status_transition(
    current_status: ClaimStatus,
    next_status: ClaimStatus,
) -> None:
    allowed = _CLAIM_STATUS_TRANSITIONS.get(current_status, {current_status})
    if next_status not in allowed:
        raise ValidationError(
            f"Claim status cannot transition from {current_status.value} to {next_status.value}."
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


def _unique_strings(values: Iterable[str] | None, field_name: str) -> list[str]:
    if not values:
        return []
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        _ensure_non_empty(value, field_name)
        cleaned = value.strip()
        if cleaned in seen:
            raise ValidationError(f"Duplicate {field_name}.")
        seen.add(cleaned)
        unique.append(cleaned)
    return unique


def _normalize_dataset_files(files: Iterable[DatasetFile]) -> list[DatasetFile]:
    normalized: list[DatasetFile] = []
    seen: set[str] = set()
    for file in files:
        _ensure_non_empty(file.path, "file.path")
        _ensure_non_empty(file.checksum, "file.checksum")
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
        metadata=manifest_input.metadata,
        nwb_metadata=manifest_input.nwb_metadata,
        bids_metadata=manifest_input.bids_metadata,
        note_ids=manifest_input.note_ids,
        extraction_provenance=manifest_input.extraction_provenance,
        source_session_id=manifest_input.source_session_id,
    )


def _normalize_commit_metadata(metadata: dict[str, str] | None) -> dict[str, str]:
    if not metadata:
        return {}
    cleaned: dict[str, str] = {}
    for key, value in metadata.items():
        _ensure_non_empty(key, "metadata key")
        cleaned_key = str(key).strip()
        cleaned_value = value.strip() if isinstance(value, str) else str(value)
        cleaned[cleaned_key] = cleaned_value
    return cleaned


def _manifest_input_from_commit(manifest: DatasetCommitManifest) -> DatasetCommitManifestInput:
    return DatasetCommitManifestInput(
        files=list(manifest.files),
        metadata=dict(manifest.metadata),
        nwb_metadata=dict(manifest.nwb_metadata),
        bids_metadata=dict(manifest.bids_metadata),
        note_ids=list(manifest.note_ids),
        extraction_provenance=list(manifest.extraction_provenance),
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
        metadata=manifest_input.metadata,
        nwb_metadata=manifest_input.nwb_metadata,
        bids_metadata=manifest_input.bids_metadata,
        note_ids=manifest_input.note_ids,
        extraction_provenance=manifest_input.extraction_provenance,
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
    return DatasetCommitManifest(
        files=_normalize_dataset_files(manifest_input.files),
        metadata=base_metadata,
        nwb_metadata=nwb_metadata,
        bids_metadata=bids_metadata,
        note_ids=_unique_ids(manifest_input.note_ids),
        extraction_provenance=_unique_strings(
            manifest_input.extraction_provenance,
            "extraction_provenance",
        ),
        question_links=list(question_links),
        source_session_id=manifest_input.source_session_id,
    )


def _validate_commit_hash(provided: str | None, expected: str) -> None:
    if provided is None:
        return
    _ensure_non_empty(provided, "commit_hash")
    if provided.strip() != expected:
        raise ValidationError("commit_hash must match content-addressed manifest hash.")


_ROLE_ORDER = {QuestionLinkRole.PRIMARY.value: 0, QuestionLinkRole.SECONDARY.value: 1}


def _manifest_payload(manifest: DatasetCommitManifest) -> dict[str, object]:
    files = sorted(
        ({"path": file.path, "checksum": file.checksum} for file in manifest.files),
        key=lambda item: (item["path"], item["checksum"]),
    )
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
    extraction_provenance = sorted(manifest.extraction_provenance)
    metadata = {key: manifest.metadata[key] for key in sorted(manifest.metadata)}
    nwb_metadata = {key: manifest.nwb_metadata[key] for key in sorted(manifest.nwb_metadata)}
    bids_metadata = {key: manifest.bids_metadata[key] for key in sorted(manifest.bids_metadata)}
    return {
        "files": files,
        "metadata": metadata,
        "nwb_metadata": nwb_metadata,
        "bids_metadata": bids_metadata,
        "question_links": links,
        "note_ids": note_ids,
        "extraction_provenance": extraction_provenance,
        "source_session_id": str(manifest.source_session_id)
        if manifest.source_session_id
        else None,
    }


def _compute_commit_hash(manifest: DatasetCommitManifest) -> str:
    payload = _manifest_payload(manifest)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _build_extracted_entity(label: str, confidence: float, provenance: str) -> ExtractedEntity:
    _ensure_non_empty(label, "label")
    if not 0.0 <= confidence <= 1.0:
        raise ValidationError("confidence must be between 0 and 1.")
    _ensure_non_empty(provenance, "provenance")
    return ExtractedEntity(
        label=label.strip(),
        confidence=confidence,
        provenance=provenance.strip(),
    )


@dataclass(frozen=True)
class _TagMapping:
    vocabulary: str
    term_label: str
    match_confidence: float = 1.0
    term_id: str | None = None


_DEFAULT_TAG_MAP: dict[str, list[_TagMapping]] = {
    "neuron": [
        _TagMapping(vocabulary="NIFSTD", term_label="Neuron", match_confidence=0.95),
        _TagMapping(vocabulary="NCIT", term_label="Neuron", match_confidence=0.9),
    ],
    "astrocyte": [_TagMapping(vocabulary="NIFSTD", term_label="Astrocyte", match_confidence=0.93)],
    "hippocampus": [
        _TagMapping(vocabulary="UBERON", term_label="Hippocampus", match_confidence=0.92)
    ],
    "patch clamp": [_TagMapping(vocabulary="OBI", term_label="Patch clamp", match_confidence=0.88)],
}


def _build_entity_tag_suggestion(
    *,
    entity_label: str,
    term: _TagMapping,
    extracted_confidence: float,
    provenance: str,
) -> EntityTagSuggestion:
    _ensure_non_empty(entity_label, "entity_label")
    _ensure_non_empty(term.vocabulary, "vocabulary")
    _ensure_non_empty(term.term_label, "term_label")
    _ensure_non_empty(provenance, "provenance")
    if not 0.0 <= extracted_confidence <= 1.0:
        raise ValidationError("extracted_confidence must be between 0 and 1.")
    if not 0.0 <= term.match_confidence <= 1.0:
        raise ValidationError("match_confidence must be between 0 and 1.")
    term_id = term.term_id or f"{term.vocabulary}:{_slugify_label(term.term_label)}"
    confidence = min(1.0, extracted_confidence * term.match_confidence)
    return EntityTagSuggestion(
        suggestion_id=uuid4(),
        entity_label=entity_label.strip(),
        vocabulary=term.vocabulary.strip(),
        term_id=term_id,
        term_label=term.term_label.strip(),
        confidence=confidence,
        provenance=provenance.strip(),
    )


def _build_note_tag_provenance(note_id: UUID) -> str:
    return f"note:{note_id}|tag-mapper:v1"


def _normalize_entity_label(label: str) -> str:
    cleaned = label.strip().casefold()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[_\-]+", " ", cleaned)
    return cleaned.strip()


def _slugify_label(label: str) -> str:
    cleaned = _normalize_entity_label(label)
    slug = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
    return slug or "term"


def _resolve_tag_mappings(label: str, mapping: dict[str, list[_TagMapping]]) -> list[_TagMapping]:
    normalized = _normalize_entity_label(label)
    keys_to_try = [normalized]
    if normalized.endswith("s") and len(normalized) > 1:
        keys_to_try.append(normalized[:-1])
    resolved: list[_TagMapping] = []
    for key in keys_to_try:
        resolved.extend(mapping.get(key, []))
    if not resolved:
        for key, terms in mapping.items():
            if key and key in normalized:
                resolved.extend(terms)
    return _dedupe_tag_mappings(resolved)


def _dedupe_tag_mappings(terms: Iterable[_TagMapping]) -> list[_TagMapping]:
    seen: set[tuple[str, str, str | None]] = set()
    unique: list[_TagMapping] = []
    for term in terms:
        key = (term.vocabulary.casefold(), term.term_label.casefold(), term.term_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(term)
    return unique


def _tag_suggestion_key(suggestion: EntityTagSuggestion) -> tuple[str, str, str, str]:
    return (
        suggestion.entity_label.casefold(),
        suggestion.vocabulary.casefold(),
        suggestion.term_id.casefold(),
        suggestion.term_label.casefold(),
    )


_QUESTION_PREFIX_RE = re.compile(r"^\s*(?:q|question)\s*[:\-]\s*(.+)$", re.IGNORECASE)
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")
_QUESTION_SENTENCE_RE = re.compile(r"[^?\n]*\?")


def _note_text_for_extraction(note: Note) -> str:
    if note.transcribed_text:
        return note.transcribed_text
    if note.raw_asset is not None:
        return ""
    return note.raw_content


def _extract_question_candidates(text: str) -> list[str]:
    if not text:
        return []
    candidates: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stripped = _BULLET_PREFIX_RE.sub("", stripped).strip()
        prefix_match = _QUESTION_PREFIX_RE.match(stripped)
        if prefix_match:
            candidate = _clean_question_candidate(prefix_match.group(1))
            if candidate:
                candidates.append(candidate)
            continue
        if "?" in stripped:
            for match in _QUESTION_SENTENCE_RE.findall(stripped):
                candidate = _clean_question_candidate(match)
                if candidate:
                    candidates.append(candidate)
    return _dedupe_casefold(candidates)


def _clean_question_candidate(candidate: str) -> str | None:
    cleaned = candidate.strip()
    if not cleaned:
        return None
    if cleaned.endswith(".") and "?" not in cleaned:
        cleaned = cleaned[:-1].strip()
    alpha_count = sum(1 for char in cleaned if char.isalpha())
    if alpha_count < 3:
        return None
    return cleaned


def _dedupe_casefold(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return unique


def _build_note_provenance(note_id: UUID, *, backend_name: str) -> str:
    return f"note:{note_id}|question-extractor:v2|backend:{backend_name}"
