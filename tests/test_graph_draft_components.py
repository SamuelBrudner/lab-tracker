from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

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
from lab_tracker.services.graph_draft_context import GraphContextBuilder
from lab_tracker.services.graph_draft_validation import GraphPatchValidator


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
    )

    assert result.raw_content == "Resolved note"
    assert captured["project_id"] == project_id
    assert captured["targets"] == [
        EntityRef(entity_type=EntityType.QUESTION, entity_id=question_id)
    ]
    assert captured["actor"] is None
