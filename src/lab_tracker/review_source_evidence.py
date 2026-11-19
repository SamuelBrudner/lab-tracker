"""Resolve proposal citations against the source snapshot saved with the draft."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import BaseModel

if TYPE_CHECKING:
    from lab_tracker.models import GraphChangeOperation, GraphChangeSet

EvidenceStatus = Literal[
    "exact_excerpt", "source_context", "stored_artifact", "unverified", "ambiguous"
]
ExcerptKind = Literal["attachment_text", "note_text", "transcript"]
_TEXT_FIELDS: tuple[tuple[str, ExcerptKind], ...] = (
    ("raw_asset_text", "attachment_text"),
    ("raw_content_preview", "note_text"),
    ("transcript_text", "transcript"),
)
_EXCERPT_LIMIT = 240


class ReviewSourceEvidence(BaseModel):
    status: EvidenceStatus
    note_id: UUID | None = None
    filename: str | None = None
    artifact_id: UUID | None = None
    excerpt: str | None = None
    excerpt_kind: ExcerptKind | None = None
    text_shortened: bool = False


def source_evidence_by_operation(draft: GraphChangeSet) -> dict[UUID, list[ReviewSourceEvidence]]:
    artifacts = _source_artifacts(draft)
    return {
        operation.operation_id: _operation_evidence(operation, artifacts, draft)
        for operation in draft.operations
    }


def proposal_source_evidence(
    draft: GraphChangeSet, operation: GraphChangeOperation
) -> list[ReviewSourceEvidence]:
    return _operation_evidence(operation, _source_artifacts(draft), draft)


def _source_artifacts(draft: GraphChangeSet) -> dict[str, dict[str, Any] | None]:
    ids = {str(note_id) for note_id in (draft.source_note_id, *draft.source_note_ids)}
    artifacts: dict[str, dict[str, Any] | None] = dict.fromkeys(ids)
    for item in draft.context_packet.get("source_artifacts") or []:
        if not isinstance(item, dict) or item.get("project_id") != str(draft.project_id):
            continue
        note_id = item.get("note_id")
        if isinstance(note_id, str) and note_id in ids:
            artifacts[note_id] = item
    return artifacts


def _source_ids(ref: dict[str, Any], allowed: dict[str, Any]) -> list[str]:
    ids = ref.get("source_note_ids")
    if ids is None:
        single = ref.get("source_note_id") or ref.get("note_id")
        ids = [single] if single else list(allowed)
    if not isinstance(ids, list) or any(
        not isinstance(note_id, str) or note_id not in allowed for note_id in ids
    ):
        return []
    return list(dict.fromkeys(ids))


def _saved_text(artifact: dict[str, Any]) -> Iterator[tuple[str, ExcerptKind, bool]]:
    for field, kind in _TEXT_FIELDS:
        value = artifact.get(field)
        if isinstance(value, str) and value.strip():
            yield value, kind, artifact.get(f"{field}_truncated") is True


def _optional_uuid(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _evidence_at(
    artifact: dict[str, Any], status: EvidenceStatus, text: tuple[str, ExcerptKind, bool] | None
) -> ReviewSourceEvidence:
    filename = artifact.get("filename")
    return ReviewSourceEvidence(
        status=status,
        note_id=UUID(artifact["note_id"]),
        artifact_id=_optional_uuid(artifact.get("artifact_id")),
        filename=filename if isinstance(filename, str) else None,
        excerpt=text[0][:_EXCERPT_LIMIT] if text else None,
        excerpt_kind=text[1] if text else None,
        text_shortened=bool(text and (text[2] or len(text[0]) > _EXCERPT_LIMIT)),
    )


def _resolve_ref(
    ref: dict[str, Any], artifacts: dict[str, dict[str, Any] | None]
) -> ReviewSourceEvidence:
    ids = _source_ids(ref, artifacts)
    resolution = ref.get("source_note_ids_resolution")
    if resolution == "ambiguous_bundle" or (len(ids) > 1 and resolution != "explicit"):
        return ReviewSourceEvidence(status="ambiguous")
    cited = [artifact for note_id in ids if (artifact := artifacts[note_id]) is not None]
    raw_quote = ref.get("quote")
    quote = raw_quote.strip() if isinstance(raw_quote, str) else ""
    if quote:
        matches = []
        for artifact in cited:
            for text, kind, shortened in _saved_text(artifact):
                if quote in text:
                    matches.append((artifact, (quote, kind, shortened)))
                    break
        if len(matches) == 1:
            artifact, matched_text = matches[0]
            return _evidence_at(artifact, "exact_excerpt", matched_text)
        if len(matches) > 1:
            return ReviewSourceEvidence(status="ambiguous")
    if len(ids) == 1 and len(cited) == 1:
        artifact = cited[0]
        context = next(_saved_text(artifact), None)
        if quote:
            return _evidence_at(artifact, "unverified", context)
        if _optional_uuid(artifact.get("artifact_id")):
            return _evidence_at(artifact, "stored_artifact", context)
        if context is not None:
            return _evidence_at(artifact, "source_context", context)
        return _evidence_at(artifact, "unverified", None)
    return ReviewSourceEvidence(status="ambiguous" if len(ids) > 1 else "unverified")


def _operation_evidence(
    operation: GraphChangeOperation,
    artifacts: dict[str, dict[str, Any] | None],
    draft: GraphChangeSet,
) -> list[ReviewSourceEvidence]:
    attachment = draft.context_packet.get("review_attachment_evidence")
    if isinstance(attachment, dict) and attachment.get("status") == "unavailable":
        return [ReviewSourceEvidence(status="ambiguous")]
    if not operation.source_refs:
        return [ReviewSourceEvidence(status="unverified")]
    return [_resolve_ref(ref, artifacts) for ref in operation.source_refs]
