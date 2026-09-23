"""Rule-based link proposals from identifiers a capture already carries.

A figure saved inside ``run_context`` records the git commit it was made
from; a repo or HPC event records its commit; a watched file records the
session its folder named. When one of those identifiers matches exactly one
entity in the project, the link is a fact, not an inference. This pass turns
such facts into ordinary graph-draft operations (confidence 1.0, marked
``exact_id_match`` in their source references) so the daily review still
decides, but the model is never asked to guess what an id already says.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Protocol
from uuid import UUID, uuid4

from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    Analysis,
    AnalysisStatus,
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeSet,
    GraphDraftSemanticType,
    Note,
    Session,
    encode_session_link_code,
)
from lab_tracker.services.graph_draft_generation_ports import GenerationPatchValidator

logger = logging.getLogger(__name__)

ID_MATCH_BASIS = "exact_id_match"
COMMIT_METADATA_KEYS = ("run_git_commit", "repo_git_commit", "hpc_git_commit", "git_commit")
SESSION_METADATA_KEYS = ("watch_session_id", "capture_session_id")
_MIN_COMMIT_PREFIX = 7


class IdMatchSources(Protocol):
    """Project entities an identifier can resolve to."""

    def committed_analyses(self, project_id: UUID) -> list[Analysis]: ...

    def project_sessions(self, project_id: UUID) -> list[Session]: ...


class RepositoryIdMatchSources:
    """Sources backed by the analysis and session services' repositories.

    The repository is resolved per call, never at construction: a service's
    repository is request-scoped and may not exist yet when the facade is
    built.
    """

    def __init__(self, *, analyses: Any, sessions: Any) -> None:
        self._analyses = analyses
        self._sessions = sessions

    def committed_analyses(self, project_id: UUID) -> list[Analysis]:
        analyses, _total = self._analyses.repository.query_analyses(
            project_id=project_id,
            status=AnalysisStatus.COMMITTED.value,
            limit=None,
            offset=0,
        )
        return list(analyses)

    def project_sessions(self, project_id: UUID) -> list[Session]:
        sessions, _total = self._sessions.repository.query_sessions(
            project_id=project_id,
            limit=None,
            offset=0,
        )
        return list(sessions)


def _commit_matches(commit: str, code_version: str) -> bool:
    left = commit.strip().lower()
    right = code_version.strip().lower()
    if len(left) < _MIN_COMMIT_PREFIX or len(right) < _MIN_COMMIT_PREFIX:
        return False
    return left == right or left.startswith(right) or right.startswith(left)


def _note_targets(note: Note) -> list[dict[str, str]]:
    return [
        {"entity_type": target.entity_type.value, "entity_id": str(target.entity_id)}
        for target in note.targets
    ]


def _matches_for_note(
    note: Note,
    *,
    analyses: Sequence[Analysis],
    sessions: Sequence[Session],
) -> list[dict[str, Any]]:
    """Exact-id matches for one note, each with the evidence that proves it."""

    metadata = note.metadata or {}
    already = {(target.entity_type, target.entity_id) for target in note.targets}
    matches: list[dict[str, Any]] = []
    seen: set[tuple[EntityType, UUID]] = set()

    for key in SESSION_METADATA_KEYS:
        value = str(metadata.get(key) or "").strip()
        if not value:
            continue
        try:
            session_id = UUID(value)
        except ValueError:
            continue
        session = next((item for item in sessions if item.session_id == session_id), None)
        if session is None or (EntityType.SESSION, session_id) in already:
            continue
        if (EntityType.SESSION, session_id) in seen:
            continue
        seen.add((EntityType.SESSION, session_id))
        matches.append(
            {
                "entity_type": EntityType.SESSION,
                "entity_id": session_id,
                "key": key,
                "value": value,
                "description": (
                    f"{key} names session {encode_session_link_code(session_id)} exactly"
                ),
            }
        )

    for key in COMMIT_METADATA_KEYS:
        value = str(metadata.get(key) or "").strip()
        if not value:
            continue
        candidates = [item for item in analyses if _commit_matches(value, item.code_version)]
        if len(candidates) != 1:
            # No match is silence; several matches are an ambiguity the model
            # (and the person) can weigh, never a rule-based claim.
            continue
        analysis = candidates[0]
        pair = (EntityType.ANALYSIS, analysis.analysis_id)
        if pair in already or pair in seen:
            continue
        seen.add(pair)
        matches.append(
            {
                "entity_type": EntityType.ANALYSIS,
                "entity_id": analysis.analysis_id,
                "key": key,
                "value": value,
                "description": (
                    f"{key} {value[:12]} matches the committed analysis "
                    f"code_version {analysis.code_version[:12]} exactly"
                ),
            }
        )
        break
    return matches


def propose_id_match_operations(
    notes: Sequence[Note],
    *,
    change_set: GraphChangeSet,
    sources: IdMatchSources,
    validator: GenerationPatchValidator,
    starting_sequence: int,
) -> tuple[list[GraphChangeOperation], dict[str, Any]]:
    """Build one link operation per note whose ids resolve to project entities.

    The payload carries the note's existing targets plus every matched
    entity, because a note update replaces the whole target list. Returns the
    operations and a small summary for the change set's context packet.
    """

    project_id = change_set.project_id
    analyses = sources.committed_analyses(project_id)
    sessions = sources.project_sessions(project_id)
    operations: list[GraphChangeOperation] = []
    summary: dict[str, Any] = {"basis": ID_MATCH_BASIS, "proposed": [], "skipped": []}
    sequence = starting_sequence
    for note in notes:
        matches = _matches_for_note(note, analyses=analyses, sessions=sessions)
        if not matches:
            continue
        targets = _note_targets(note)
        for match in matches:
            targets.append(
                {
                    "entity_type": match["entity_type"].value,
                    "entity_id": str(match["entity_id"]),
                }
            )
        semantic = (
            GraphDraftSemanticType.LINK_NOTE_TO_SESSION
            if any(match["entity_type"] == EntityType.SESSION for match in matches)
            else GraphDraftSemanticType.LINK_NOTE_TO_ANALYSIS
        )
        descriptions = "; ".join(str(match["description"]) for match in matches)
        operation = GraphChangeOperation(
            operation_id=uuid4(),
            change_set_id=change_set.change_set_id,
            sequence=sequence,
            op=GraphChangeOp.UPDATE,
            entity_type=EntityType.NOTE,
            semantic_type=semantic,
            target_entity_id=note.note_id,
            client_ref=f"id-match-{note.note_id}",
            payload={"targets": targets},
            rationale=(
                f"Exact id match, no model inference: {descriptions}. "
                "Accepting links the capture to what its own metadata names."
            ),
            confidence=1.0,
            source_refs=[
                {
                    "label": "capture metadata",
                    "quote": f"{match['key']}={match['value']}",
                    "region": None,
                    "source_note_ids": [str(note.note_id)],
                    "source_note_ids_resolution": "explicit",
                    "basis": ID_MATCH_BASIS,
                }
                for match in matches
            ],
        )
        try:
            validator.validate_operation(operation, operation.payload)
        except ValidationError as exc:
            summary["skipped"].append({"note_id": str(note.note_id), "reason": str(exc)})
            logger.info("id-match proposal skipped for note %s: %s", note.note_id, exc)
            continue
        operations.append(operation)
        summary["proposed"].append(
            {
                "note_id": str(note.note_id),
                "targets": [
                    {
                        "entity_type": match["entity_type"].value,
                        "entity_id": str(match["entity_id"]),
                    }
                    for match in matches
                ],
            }
        )
        sequence += 1
    return operations, summary


__all__ = [
    "COMMIT_METADATA_KEYS",
    "ID_MATCH_BASIS",
    "IdMatchSources",
    "RepositoryIdMatchSources",
    "SESSION_METADATA_KEYS",
    "propose_id_match_operations",
]
