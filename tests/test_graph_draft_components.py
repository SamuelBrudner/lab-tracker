from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from lab_tracker.errors import ValidationError
from lab_tracker.graph_drafting import (
    ANALYSIS_PROMPT_VERSION,
    BATCH_PROMPT_VERSION,
    PROMPT_VERSION,
    GraphDraftingError,
    _analysis_instructions,
    _batch_instructions,
    _instructions,
    graph_patch_response_schema,
)
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


def _change_set(
    project_id: UUID,
    *,
    source_note_ids: list[UUID] | None = None,
) -> GraphChangeSet:
    source_ids = list(source_note_ids or [])
    return GraphChangeSet(
        change_set_id=uuid4(),
        project_id=project_id,
        source_note_id=source_ids[0] if source_ids else uuid4(),
        source_note_ids=source_ids,
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

    change_set = _change_set(project_id)
    operations = validator.operations_from_graph_patch(change_set, graph_patch)

    assert operations[0].entity_type == EntityType.QUESTION
    assert operations[0].semantic_type == GraphDraftSemanticType.SUGGEST_NEW_QUESTION
    assert operations[0].source_refs[0]["source_note_ids"] == [str(change_set.source_note_id)]
    assert operations[0].source_refs[0]["source_note_ids_resolution"] == "single_source_fallback"
    assert seen_refs == [(EntityType.PROJECT, project_id)]


def test_graph_patch_response_schema_requires_non_empty_source_note_ids() -> None:
    schema = graph_patch_response_schema()
    source_ref_schema = schema["properties"]["operations"]["items"]["properties"]["source_refs"][
        "items"
    ]
    source_note_ids_schema = source_ref_schema["properties"]["source_note_ids"]

    assert "source_note_ids" in source_ref_schema["required"]
    assert source_note_ids_schema["minItems"] == 1
    assert "uniqueItems" not in source_note_ids_schema


def test_graph_draft_prompt_versions_and_source_ref_contract_are_updated() -> None:
    assert PROMPT_VERSION == "multimodal-graph-draft-v2"
    assert BATCH_PROMPT_VERSION == "daily-batch-graph-draft-v4"
    assert ANALYSIS_PROMPT_VERSION == "analysis-graph-draft-v2"
    for instructions in (_instructions(), _batch_instructions(), _analysis_instructions()):
        assert "source_note_ids" in instructions
        assert "never invent" in instructions.lower()
    assert "non-empty raw_content" in _batch_instructions()
    assert "metadata.title" in _batch_instructions()


@pytest.mark.parametrize("content_field", ["text", "content", "body"])
def test_graph_patch_validator_normalizes_note_content_aliases(
    content_field: str,
) -> None:
    project_id = uuid4()
    operation = _patch_operation(
        project_id=project_id,
        entity_type="note",
        semantic_type="create_note",
        payload={
            "project_id": str(project_id),
            content_field: "Canonical note body",
        },
    )
    validator = GraphPatchValidator(
        get_graph_entity=lambda entity_type, entity_id: Project(
            project_id=entity_id,
            name="Project",
        )
    )

    operations = validator.operations_from_graph_patch(
        _change_set(project_id),
        {
            "summary": "valid",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [operation],
        },
    )

    assert operations[0].payload == {
        "project_id": str(project_id),
        "raw_content": "Canonical note body",
    }


def test_graph_patch_validator_preserves_note_title_as_metadata() -> None:
    project_id = uuid4()
    operation = _patch_operation(
        project_id=project_id,
        entity_type="note",
        semantic_type="create_note",
        payload={
            "project_id": str(project_id),
            "raw_content": "Canonical note body",
            "title": "Human-facing label",
            "metadata": {"source": "batch"},
        },
    )
    validator = GraphPatchValidator(
        get_graph_entity=lambda entity_type, entity_id: Project(
            project_id=entity_id,
            name="Project",
        )
    )

    operations = validator.operations_from_graph_patch(
        _change_set(project_id),
        {
            "summary": "valid",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [operation],
        },
    )

    assert operations[0].payload == {
        "project_id": str(project_id),
        "raw_content": "Canonical note body",
        "metadata": {"source": "batch", "title": "Human-facing label"},
    }


def test_graph_patch_validator_preserves_explicit_source_note_ids() -> None:
    project_id = uuid4()
    source_note_ids = [uuid4(), uuid4()]
    operation = _patch_operation(project_id=project_id)
    operation["source_refs"] = [
        {
            "label": "second figure",
            "quote": "panel B",
            "region": None,
            "source_note_ids": [str(source_note_ids[1])],
        }
    ]
    validator = GraphPatchValidator(
        get_graph_entity=lambda entity_type, entity_id: Project(
            project_id=entity_id, name="Project"
        )
    )

    operations = validator.operations_from_graph_patch(
        _change_set(project_id, source_note_ids=source_note_ids),
        {
            "summary": "valid",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [operation],
        },
    )

    assert operations[0].source_refs[0]["source_note_ids"] == [str(source_note_ids[1])]
    assert operations[0].source_refs[0]["source_note_ids_resolution"] == "explicit"


@pytest.mark.parametrize("legacy_key", ["source_note_id", "note_id"])
def test_graph_patch_validator_normalizes_legacy_singular_source_note_id(legacy_key: str) -> None:
    project_id = uuid4()
    source_note_id = uuid4()
    operation = _patch_operation(project_id=project_id)
    operation["source_refs"] = [
        {"label": "legacy", "quote": "panel A", "region": None, legacy_key: str(source_note_id)}
    ]
    validator = GraphPatchValidator(
        get_graph_entity=lambda entity_type, entity_id: Project(
            project_id=entity_id, name="Project"
        )
    )

    operations = validator.operations_from_graph_patch(
        _change_set(project_id, source_note_ids=[source_note_id]),
        {
            "summary": "valid",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [operation],
        },
    )

    source_ref = operations[0].source_refs[0]
    assert legacy_key not in source_ref
    assert source_ref["source_note_ids"] == [str(source_note_id)]
    assert source_ref["source_note_ids_resolution"] == "explicit"


def test_graph_patch_validator_keeps_missing_multi_source_ref_ambiguous() -> None:
    project_id = uuid4()
    source_note_ids = [uuid4(), uuid4()]
    operation = _patch_operation(project_id=project_id)
    operation["source_refs"] = [{"label": "bundle", "quote": "result", "region": None}]
    validator = GraphPatchValidator(
        get_graph_entity=lambda entity_type, entity_id: Project(
            project_id=entity_id, name="Project"
        )
    )

    operations = validator.operations_from_graph_patch(
        _change_set(project_id, source_note_ids=source_note_ids),
        {
            "summary": "valid",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [operation],
        },
    )

    source_ref = operations[0].source_refs[0]
    assert source_ref["source_note_ids"] == [str(note_id) for note_id in source_note_ids]
    assert source_ref["source_note_ids_resolution"] == "ambiguous_bundle"


@pytest.mark.parametrize("source_note_ids", [[], None])
def test_graph_patch_validator_rejects_invalid_explicit_source_note_ids(
    source_note_ids: list[str] | None,
) -> None:
    project_id = uuid4()
    allowed_source_note_id = uuid4()
    operation = _patch_operation(project_id=project_id)
    operation["source_refs"] = [
        {"label": "figure", "quote": "result", "region": None, "source_note_ids": source_note_ids}
    ]
    validator = GraphPatchValidator(
        get_graph_entity=lambda entity_type, entity_id: Project(
            project_id=entity_id, name="Project"
        )
    )

    with pytest.raises(GraphDraftingError, match="non-empty list"):
        validator.operations_from_graph_patch(
            _change_set(project_id, source_note_ids=[allowed_source_note_id]),
            {
                "summary": "invalid",
                "uncertain_fields": [],
                "clarification_requests": [],
                "operations": [operation],
            },
        )


def test_graph_patch_validator_rejects_cross_draft_and_duplicate_source_note_ids() -> None:
    project_id = uuid4()
    allowed_source_note_id = uuid4()
    validator = GraphPatchValidator(
        get_graph_entity=lambda entity_type, entity_id: Project(
            project_id=entity_id, name="Project"
        )
    )
    for ids, message in (
        ([str(uuid4())], "outside this draft's source notes"),
        ([str(allowed_source_note_id)] * 2, "must be unique"),
    ):
        operation = _patch_operation(project_id=project_id)
        operation["source_refs"] = [
            {"label": "figure", "quote": "result", "region": None, "source_note_ids": ids}
        ]
        with pytest.raises(GraphDraftingError, match=message):
            validator.operations_from_graph_patch(
                _change_set(project_id, source_note_ids=[allowed_source_note_id]),
                {
                    "summary": "invalid",
                    "uncertain_fields": [],
                    "clarification_requests": [],
                    "operations": [operation],
                },
            )


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


def test_graph_patch_validator_rejects_empty_update_payload() -> None:
    project_id = uuid4()
    operation = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=1,
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.QUESTION,
        semantic_type=GraphDraftSemanticType.UPDATE_ENTITY,
        target_entity_id=uuid4(),
        payload={},
        rationale="An empty update must never rewrite provenance.",
    )
    validator = GraphPatchValidator(
        get_graph_entity=lambda entity_type, entity_id: Project(
            project_id=project_id,
            name="Project",
        )
    )

    with pytest.raises(ValidationError, match="must include at least one field"):
        validator.validate_operation(operation, operation.payload)


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


def test_graph_patch_applier_preserves_update_field_presence_and_explicit_null() -> None:
    project_id = uuid4()
    question_id = uuid4()
    captured: dict[str, Any] = {}

    class Questions:
        def update_question(self, entity_id: UUID, **kwargs: Any) -> Project:
            captured["entity_id"] = entity_id
            captured["kwargs"] = kwargs
            return Project(project_id=project_id, name="Placeholder")

    applier = GraphPatchApplier(
        projects=SimpleNamespace(),
        questions=Questions(),
        notes=SimpleNamespace(),
        sessions=SimpleNamespace(),
        datasets=SimpleNamespace(),
        analyses=SimpleNamespace(),
        claims=SimpleNamespace(),
        visualizations=SimpleNamespace(),
    )
    change_set = _change_set(project_id)
    operation = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=change_set.change_set_id,
        sequence=1,
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.QUESTION,
        semantic_type=GraphDraftSemanticType.UPDATE_ENTITY,
        target_entity_id=question_id,
        payload={"hypothesis": None},
        rationale="Clear only the hypothesis.",
    )

    applier.apply_graph_operation(
        operation,
        ref_map={},
        actor=None,
        change_set=change_set,
    )

    assert captured["entity_id"] == question_id
    assert captured["kwargs"]["hypothesis"] is None
    assert "text" not in captured["kwargs"]
    assert "status" not in captured["kwargs"]


def test_graph_patch_applier_forwards_nullable_project_group_id() -> None:
    project_id = uuid4()
    captured: dict[str, Any] = {}

    class Projects:
        def update_project(self, entity_id: UUID, **kwargs: Any) -> Project:
            captured.update({"entity_id": entity_id, **kwargs})
            return Project(project_id=entity_id, name="Project")

    applier = GraphPatchApplier(
        projects=Projects(),
        questions=SimpleNamespace(),
        notes=SimpleNamespace(),
        sessions=SimpleNamespace(),
        datasets=SimpleNamespace(),
        analyses=SimpleNamespace(),
        claims=SimpleNamespace(),
        visualizations=SimpleNamespace(),
    )
    change_set = _change_set(project_id)
    operation = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=change_set.change_set_id,
        sequence=1,
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.PROJECT,
        semantic_type=GraphDraftSemanticType.UPDATE_ENTITY,
        target_entity_id=project_id,
        payload={"group_id": None},
        rationale="Detach from the group.",
    )

    applier.apply_graph_operation(
        operation,
        ref_map={},
        actor=None,
        change_set=change_set,
    )

    assert captured["entity_id"] == project_id
    assert captured["group_id"] is None
    assert "name" not in captured


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


def test_batch_instructions_are_narrative_first_with_terse_capture_guardrail() -> None:
    instructions = _batch_instructions()
    lowered = instructions.lower()
    # Narrative-first: the summary becomes a day-narrative, not a one-liner.
    assert "narrative of the user's day" in lowered
    assert "in time order" in lowered
    # Terse-capture guardrail folded into the narrative frame.
    assert "a bare label or identifier is not a finding" in lowered
    assert "rig 2 fly 12" in lowered
    assert "clarification_requests" in instructions
    # Meeting notes still get fleshed out, but only where content exists.
    assert "meeting" in lowered
    assert "flesh out what the meeting discussed" in lowered
    assert "never fabricate content for an identifier-only capture" in lowered
    # Stays subordinate to the supported-changes guardrail.
    assert "supported by the source artifacts" in instructions
    # The summary contract changed (now a narrative), so the version bumps.
    assert BATCH_PROMPT_VERSION == "daily-batch-graph-draft-v4"
