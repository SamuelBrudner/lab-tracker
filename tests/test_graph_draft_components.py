from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from lab_tracker.graph_drafting import BATCH_PROMPT_VERSION, _batch_instructions
from lab_tracker.models import (
    EntityRef,
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeSet,
    GraphDraftMode,
    GraphDraftSemanticType,
    Note,
    Project,
)
from lab_tracker.services.graph_draft_applier import GraphPatchApplier
from lab_tracker.services.graph_draft_context import (
    GraphContextBuilder,
    _compact_note,
    _graph_batch_context_summary,
    _source_artifact_packet,
)
from lab_tracker.services.graph_draft_validation import GraphPatchValidator
from lab_tracker.services.shared import MEETING_NOTE_TYPE, NOTE_TYPE_METADATA_KEY, is_meeting_note


def _change_set(project_id: UUID) -> GraphChangeSet:
    return GraphChangeSet(
        change_set_id=uuid4(),
        project_id=project_id,
        source_note_id=uuid4(),
        model="fake-gpt",
        prompt_version="test",
    )


def _patch_operation(
    *,
    project_id: UUID,
    entity_type: str = "question",
    semantic_type: str = "suggest_new_question",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "client_ref": "ref-1",
        "op": "create",
        "entity_type": entity_type,
        "semantic_type": semantic_type,
        "target_entity_id": None,
        "payload_json": json.dumps(
            payload
            or {
                "project_id": str(project_id),
                "text": "Does the extracted graph draft stay valid?",
                "question_type": "descriptive",
                "status": "staged",
            }
        ),
        "rationale": "Exercise the extracted validator.",
        "confidence": 0.8,
        "source_refs": [],
    }


def test_graph_context_builder_image_only_packet_includes_summary() -> None:
    note = Note(
        note_id=uuid4(),
        project_id=uuid4(),
        raw_content="Whiteboard sketch",
    )
    builder = GraphContextBuilder(
        projects=SimpleNamespace(),
        questions=SimpleNamespace(),
        notes=SimpleNamespace(),
        sessions=SimpleNamespace(),
        datasets=SimpleNamespace(),
        analyses=SimpleNamespace(),
        claims=SimpleNamespace(),
        visualizations=SimpleNamespace(),
    )

    packet = builder.image_only_context_packet(
        note,
        source_notes=[note],
        user_hint="focus on controls",
    )

    assert packet["mode"] == GraphDraftMode.IMAGE_ONLY.value
    assert packet["source_artifacts"][0]["type"] == "text"
    assert packet["context_summary"]["counts"]["source_artifacts"] == 1
    assert "Image-only draft" in packet["warning"]


def test_graph_patch_validator_parses_operations_and_checks_payload_references() -> None:
    project_id = uuid4()
    seen_refs: list[tuple[EntityType, UUID]] = []

    def get_entity(entity_type: EntityType, entity_id: UUID) -> Project:
        seen_refs.append((entity_type, entity_id))
        return Project(project_id=entity_id, name="Project")

    validator = GraphPatchValidator(get_graph_entity=get_entity)
    graph_patch = {
        "summary": "valid",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [_patch_operation(project_id=project_id)],
    }

    operations = validator.operations_from_graph_patch(_change_set(project_id), graph_patch)

    assert operations[0].entity_type == EntityType.QUESTION
    assert operations[0].semantic_type == GraphDraftSemanticType.SUGGEST_NEW_QUESTION
    assert seen_refs == [(EntityType.PROJECT, project_id)]


def test_graph_patch_validator_allows_client_refs_during_review_validation() -> None:
    project_id = uuid4()
    seen_refs: list[tuple[EntityType, UUID]] = []

    def get_entity(entity_type: EntityType, entity_id: UUID) -> Project:
        seen_refs.append((entity_type, entity_id))
        return Project(project_id=entity_id, name="Project")

    validator = GraphPatchValidator(get_graph_entity=get_entity)
    graph_patch = {
        "summary": "valid",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [
            _patch_operation(
                project_id=project_id,
                entity_type="note",
                semantic_type="create_note",
                payload={
                    "project_id": str(project_id),
                    "raw_content": "Linked note",
                    "targets": [
                        {
                            "entity_type": "question",
                            "entity_id": {"$ref": "question-1"},
                        }
                    ],
                },
            )
        ],
    }

    operations = validator.operations_from_graph_patch(_change_set(project_id), graph_patch)

    assert operations[0].payload["targets"][0]["entity_id"] == {"$ref": "question-1"}
    assert seen_refs == [(EntityType.PROJECT, project_id)]


def test_graph_patch_applier_resolves_client_refs_before_create_service_call() -> None:
    project_id = uuid4()
    question_id = uuid4()
    captured: dict[str, Any] = {}

    class Notes:
        def create_note(
            self,
            *,
            project_id: UUID,
            raw_content: str,
            transcribed_text: str | None,
            targets: list[EntityRef],
                metadata: dict[str, str],
                status: Any,
                actor: Any,
                **_: Any,
            ) -> Note:
            captured.update(
                {
                    "project_id": project_id,
                    "raw_content": raw_content,
                    "targets": targets,
                    "status": status,
                    "actor": actor,
                }
            )
            return Note(
                note_id=uuid4(),
                project_id=project_id,
                raw_content=raw_content,
                targets=targets,
                metadata=metadata or {},
                status=status,
            )

    applier = GraphPatchApplier(
        projects=SimpleNamespace(),
        questions=SimpleNamespace(),
        notes=Notes(),
        sessions=SimpleNamespace(),
        datasets=SimpleNamespace(),
        analyses=SimpleNamespace(),
        claims=SimpleNamespace(),
        visualizations=SimpleNamespace(),
    )
    operation = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=1,
        op=GraphChangeOp.CREATE,
        entity_type=EntityType.NOTE,
        semantic_type=GraphDraftSemanticType.CREATE_NOTE,
        payload={
            "project_id": str(project_id),
            "raw_content": "Resolved note",
            "targets": [
                {
                    "entity_type": "question",
                    "entity_id": {"$ref": "question-1"},
                }
            ],
        },
    )

    result = applier.apply_graph_operation(
        operation,
        ref_map={"question-1": question_id},
        actor=None,
        change_set=_change_set(project_id),
    )

    assert result.raw_content == "Resolved note"
    assert captured["project_id"] == project_id
    assert captured["targets"] == [
        EntityRef(entity_type=EntityType.QUESTION, entity_id=question_id)
    ]
    assert captured["actor"] is None


def _meeting_note() -> Note:
    return Note(
        note_id=uuid4(),
        project_id=uuid4(),
        raw_content="Lab meeting: discussed PV inhibition follow-ups",
        metadata={NOTE_TYPE_METADATA_KEY: MEETING_NOTE_TYPE},
    )


def _plain_note() -> Note:
    return Note(
        note_id=uuid4(),
        project_id=uuid4(),
        raw_content="Bench note",
        metadata={"capture_source": "mobile"},
    )


def test_is_meeting_note_keys_off_metadata_note_type() -> None:
    assert is_meeting_note(_meeting_note()) is True
    assert is_meeting_note(_plain_note()) is False
    # A different note_type value is not a meeting.
    other = Note(
        note_id=uuid4(),
        project_id=uuid4(),
        raw_content="x",
        metadata={"note_type": "memo"},
    )
    assert is_meeting_note(other) is False


def test_compact_note_and_source_artifact_expose_is_meeting() -> None:
    meeting = _meeting_note()
    plain = _plain_note()
    assert _compact_note(meeting)["is_meeting"] is True
    assert _compact_note(plain)["is_meeting"] is False
    assert _source_artifact_packet(meeting)["is_meeting"] is True
    assert _source_artifact_packet(plain)["is_meeting"] is False


def test_graph_batch_context_summary_counts_meeting_notes() -> None:
    packet = {
        "batch_notes": [
            {"id": "a", "is_meeting": True},
            {"id": "b", "is_meeting": False},
            {"id": "c", "is_meeting": True},
        ],
        "source_artifacts": [],
        "projects": [],
        "truncated_note_count": 0,
    }
    summary = _graph_batch_context_summary(packet)
    assert summary["counts"]["meeting_notes"] == 2
    assert summary["counts"]["batch_notes"] == 3


def test_graph_change_set_meeting_note_count_reads_context_packet() -> None:
    with_meetings = GraphChangeSet(
        change_set_id=uuid4(),
        project_id=uuid4(),
        source_note_id=uuid4(),
        model="fake-gpt",
        prompt_version="test",
        context_packet={"context_summary": {"counts": {"meeting_notes": 3}}},
    )
    assert with_meetings.meeting_note_count == 3

    # Note-scoped drafts (no batch summary) and malformed packets count as zero.
    assert _change_set(uuid4()).meeting_note_count == 0
    malformed = GraphChangeSet(
        change_set_id=uuid4(),
        project_id=uuid4(),
        source_note_id=uuid4(),
        model="fake-gpt",
        prompt_version="test",
        context_packet={"context_summary": {"counts": {"meeting_notes": True}}},
    )
    assert malformed.meeting_note_count == 0


def test_batch_instructions_flesh_out_meeting_notes_without_version_bump() -> None:
    instructions = _batch_instructions()
    assert "meeting" in instructions.lower()
    assert "flesh out the scientific content" in instructions.lower()
    # Stays subordinate to the supported-changes guardrail.
    assert "supported by the source artifacts" in instructions
    # Provenance version is additive-only; it must not change.
    assert BATCH_PROMPT_VERSION == "daily-batch-graph-draft-v1"
