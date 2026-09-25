from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.graph_drafting import (
    _GRAPH_DRAFT_ENTITY_TYPES,
    ANALYSIS_PROMPT_VERSION,
    BATCH_PROMPT_VERSION,
    EXPLORATION_NODE_REQUIRED_FIELDS,
    PROMPT_VERSION,
    RETIRE_NOTE_REASON_VALUES,
    SEMANTIC_TYPES,
    GraphDraftingError,
    _analysis_instructions,
    _analysis_prompt_text,
    _batch_instructions,
    _batch_prompt_text,
    _instructions,
    _note_prompt_text,
    graph_draft_payload_contract,
    graph_patch_response_schema,
)
from lab_tracker.models import (
    REVIEW_NOTE_KEY,
    REVIEWED_AT_KEY,
    REVIEWED_BY_KEY,
    EntityOrigin,
    EntityRef,
    EntityType,
    ExplorationNode,
    ExplorationNodeType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftMode,
    GraphDraftSemanticType,
    Note,
    NoteArchiveReason,
    Project,
    Question,
    QuestionType,
    utc_now,
)
from lab_tracker.services.graph_draft_applier import GraphPatchApplier
from lab_tracker.services.graph_draft_batch_policy import BatchReviewer, context_owner_for
from lab_tracker.services.graph_draft_context import (
    CONTEXT_FIELD_CHAR_LIMIT,
    CUE_TERM_LIMIT,
    PENDING_PROPOSALS_CHAR_BUDGET,
    PENDING_PROPOSALS_ITEM_LIMIT,
    RECENT_REJECTIONS_ITEM_LIMIT,
    REVIEW_MEMORY_NOT_SCOPED_WARNING,
    REVIEW_MEMORY_NOTE_MAX_CHARS,
    GraphContextBuilder,
    _capped_text,
    _compact_note,
    _cue_terms,
    _entity_label,
    _graph_batch_context_summary,
    _source_artifact_packet,
    entity_id,
)
from lab_tracker.services.graph_draft_revision_hints import (
    REJECTED_REDRAFT_HEADING,
    REVISION_HEADING,
    compose_rejected_redraft_hint,
    compose_revise_hint,
    prior_rejection_summary,
)
from lab_tracker.services.graph_draft_validation import (
    _SEMANTIC_ALLOWED_TARGETS,
    RETIRE_NOTE_REASONS,
    GraphPatchValidator,
)
from lab_tracker.services.shared import MEETING_NOTE_TYPE, NOTE_TYPE_METADATA_KEY, is_meeting_note

NEGATIVE_KNOWLEDGE_LABELS = (
    "record_decision",
    "record_dead_end",
    "record_pivot",
    "abandon_question",
    "merge_questions",
    "retire_note",
)
# Labels added after the negative-knowledge block, in enum order.
LATER_SEMANTIC_LABELS = ("resolve_prediction",)


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
    assert packet["context_summary"]["counts"]["cue_matched"] == 0
    assert packet["context_summary"]["counts"]["exploration_nodes"] == 0
    assert packet["context_summary"]["slot_fill"] == {
        "active_floor": 0,
        "staged_fill": 0,
        "cue_match": 0,
        "recent": 0,
        "alias_match": 0,
    }
    assert packet["context_summary"]["cue_terms"] == []
    assert "Image-only draft" in packet["warning"]


def test_graph_context_builder_without_exploration_service_yields_empty_list() -> None:
    builder = GraphContextBuilder(
        projects=SimpleNamespace(),
        questions=SimpleNamespace(),
        notes=SimpleNamespace(),
        sessions=SimpleNamespace(),
        datasets=SimpleNamespace(),
        analyses=SimpleNamespace(),
        claims=SimpleNamespace(),
        visualizations=SimpleNamespace(),
        exploration=None,
    )

    assert builder._recent_exploration_nodes(uuid4()) == []


def test_cue_terms_are_rare_first_stopword_free_and_bounded() -> None:
    texts = [
        "Rig 2 fly 12 kynurenine assay",
        "the assay with plate 3",
        "kynurenine assay repeat",
    ]

    # Document frequency ascending, then longest token, then alphabetical.
    # 'the'/'with' are stopwords, 'rig'/'fly' are below the minimum length,
    # and '2'/'12'/'3' are all-digit tokens.
    assert _cue_terms(texts) == ["repeat", "plate", "kynurenine", "assay"]
    assert _cue_terms(texts) == _cue_terms(texts)

    many_tokens = " ".join(f"token{index:02d}" for index in range(30))
    assert len(_cue_terms([many_tokens])) == CUE_TERM_LIMIT
    assert _cue_terms(["the and with 12 rig"]) == []


def test_capped_text_caps_at_context_field_limit() -> None:
    assert _capped_text("x" * 300) == "x" * CONTEXT_FIELD_CHAR_LIMIT
    assert len(_capped_text("x" * 300) or "") == 240
    assert _capped_text(None) is None
    assert _capped_text("") is None


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
    assert PROMPT_VERSION == "multimodal-graph-draft-v4"
    assert BATCH_PROMPT_VERSION == "daily-batch-graph-draft-v7"
    assert ANALYSIS_PROMPT_VERSION == "analysis-graph-draft-v4"
    for instructions in (_instructions(), _batch_instructions(), _analysis_instructions()):
        assert "source_note_ids" in instructions
        assert "never invent" in instructions.lower()
        assert "trusted_api_payload_contract" in instructions
    for instructions in (_instructions(), _batch_instructions()):
        assert "selection_reason" in instructions
        assert "exploration_nodes" in instructions
        assert "cue_match" in instructions
        assert "captured_by_current_user" in instructions
    assert "non-empty raw_content" in _batch_instructions()
    assert "metadata.title" in _batch_instructions()


def test_graph_draft_payload_contract_covers_production_failure_fields() -> None:
    entities = graph_draft_payload_contract()["entities"]

    assert entities["question"]["create"]["required_fields"] == [
        "project_id",
        "text",
        "question_type",
    ]
    assert entities["question"]["create"]["controlled_values"]["question_type"] == [
        "descriptive",
        "hypothesis_driven",
        "method_dev",
        "other",
    ]
    assert entities["goal"]["create"]["required_fields"] == ["goal_type", "title"]
    assert entities["dataset"]["create"]["required_fields"] == [
        "project_id",
        "primary_question_id",
    ]
    assert entities["analysis"]["create"]["required_fields"] == [
        "project_id",
        "dataset_ids",
        "method_hash",
        "code_version",
    ]
    assert entities["note"]["create"]["required_fields"] == [
        "project_id",
        "raw_content",
    ]
    for forbidden in ("question_id", "note_id", "preview", "label"):
        assert forbidden not in entities["question"]["create"]["allowed_fields"]


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


def test_graph_patch_validator_rejects_conflicting_note_content_alias() -> None:
    project_id = uuid4()
    operation = _patch_operation(
        project_id=project_id,
        entity_type="note",
        semantic_type="create_note",
        payload={
            "project_id": str(project_id),
            "raw_content": "Canonical note body",
            "text": "Conflicting provider body",
        },
    )
    validator = GraphPatchValidator(
        get_graph_entity=lambda entity_type, entity_id: Project(
            project_id=entity_id,
            name="Project",
        )
    )

    with pytest.raises(
        GraphDraftingError,
        match="Operation payload failed API validation",
    ):
        validator.operations_from_graph_patch(
            _change_set(project_id),
            {
                "summary": "invalid",
                "uncertain_fields": [],
                "clarification_requests": [],
                "operations": [operation],
            },
        )


def test_graph_patch_validator_rejects_conflicting_note_titles() -> None:
    project_id = uuid4()
    operation = _patch_operation(
        project_id=project_id,
        entity_type="note",
        semantic_type="create_note",
        payload={
            "project_id": str(project_id),
            "raw_content": "Canonical note body",
            "title": "Conflicting provider title",
            "metadata": {"title": "Canonical metadata title"},
        },
    )
    validator = GraphPatchValidator(
        get_graph_entity=lambda entity_type, entity_id: Project(
            project_id=entity_id,
            name="Project",
        )
    )

    with pytest.raises(
        GraphDraftingError,
        match="Operation payload failed API validation",
    ):
        validator.operations_from_graph_patch(
            _change_set(project_id),
            {
                "summary": "invalid",
                "uncertain_fields": [],
                "clarification_requests": [],
                "operations": [operation],
            },
        )


def test_batch_prompt_keeps_retry_feedback_outside_untrusted_context() -> None:
    feedback = {
        "attempt": 1,
        "error": "Operation payload failed API validation: raw_content is required",
        "instruction": "Return a new complete graph patch.",
    }
    batch_context = {
        "batch_notes": [{"note_id": "source-1", "raw_content": "Observed result"}],
        "generation_retry_feedback": feedback,
    }

    prompt = _batch_prompt_text(batch_context=batch_context, user_hint=None)

    trusted_prefix, untrusted_tail = prompt.split("<untrusted_batch_context>\n", 1)
    untrusted_payload, _ = untrusted_tail.split("\n</untrusted_batch_context>", 1)
    assert "Trusted server validation feedback from the prior attempt" in trusted_prefix
    assert feedback["error"] in trusted_prefix
    assert "generation_retry_feedback" not in untrusted_payload
    assert json.loads(untrusted_payload) == {"batch_notes": batch_context["batch_notes"]}
    assert batch_context["generation_retry_feedback"] == feedback


@pytest.mark.parametrize(
    ("prompt_builder", "context_marker"),
    [
        (
            lambda context: _note_prompt_text(
                draft_mode="graph_context",
                user_hint=None,
                source_artifacts=[{"note_id": "source-1", "raw_content_preview": "x"}],
                context=context,
            ),
            "untrusted_graph_context",
        ),
        (
            lambda context: _analysis_prompt_text(
                evidence_text="result",
                project_context=context,
            ),
            "untrusted_project_context",
        ),
    ],
)
def test_non_batch_prompts_keep_retry_feedback_outside_untrusted_context(
    prompt_builder: Any,
    context_marker: str,
) -> None:
    feedback = {
        "attempt": 1,
        "error": "Operation payload failed API validation: text: Field required",
        "instruction": "Return a new complete graph patch.",
    }
    context = {
        "project": {"id": "project-1"},
        "generation_retry_feedback": feedback,
    }

    prompt = prompt_builder(context)

    trusted_prefix, untrusted_tail = prompt.split(f"<{context_marker}>\n", 1)
    untrusted_payload, _ = untrusted_tail.split(f"\n</{context_marker}>", 1)
    assert "Trusted server validation feedback from the prior attempt" in trusted_prefix
    assert feedback["error"] in trusted_prefix
    assert "generation_retry_feedback" not in untrusted_payload
    assert json.loads(untrusted_payload) == {"project": context["project"]}
    assert context["generation_retry_feedback"] == feedback


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
    # Commit captures become human-readable accounts, not hash-only entries.
    assert "what the commit appears to have accomplished" in lowered
    assert "commit hash as provenance only" in lowered
    assert "verify it against the file summary and diff" in lowered
    # Meeting notes still get fleshed out, but only where content exists.
    assert "meeting" in lowered
    assert "flesh out what the meeting discussed" in lowered
    assert "never fabricate content for an identifier-only capture" in lowered
    # Stays subordinate to the supported-changes guardrail.
    assert "supported by the source artifacts" in instructions
    # The summary contract changed (now a narrative), so the version bumps.
    assert BATCH_PROMPT_VERSION == "daily-batch-graph-draft-v7"


def test_semantic_types_match_domain_enum() -> None:
    assert list(SEMANTIC_TYPES) == [member.value for member in GraphDraftSemanticType]
    assert tuple(SEMANTIC_TYPES[-7:]) == (*NEGATIVE_KNOWLEDGE_LABELS, *LATER_SEMANTIC_LABELS)


def test_graph_draft_payload_contract_covers_exploration_nodes_and_semantic_operations() -> None:
    contract = graph_draft_payload_contract()

    create_contract = contract["entities"]["exploration_node"]["create"]
    assert {"project_id", "node_type", "title", "target"} <= set(create_contract["required_fields"])
    assert set(create_contract["controlled_values"]["node_type"]) == {
        "decision",
        "dead_end",
        "pivot",
    }
    assert "invalidates_claim_id" in contract["entities"]["exploration_node"]["update"][
        "allowed_fields"
    ]

    semantic = contract["semantic_operations"]
    assert tuple(semantic) == (*NEGATIVE_KNOWLEDGE_LABELS, *LATER_SEMANTIC_LABELS)
    for label, node_type in (
        ("record_decision", "decision"),
        ("record_dead_end", "dead_end"),
        ("record_pivot", "pivot"),
    ):
        assert semantic[label]["op"] == "create"
        assert semantic[label]["entity_type"] == "exploration_node"
        assert semantic[label]["node_type"] == node_type
        assert semantic[label]["required_fields"] == list(
            EXPLORATION_NODE_REQUIRED_FIELDS[node_type]
        )
        assert semantic[label]["target_entity_types"] == [
            "question",
            "dataset",
            "analysis",
            "claim",
        ]
    assert semantic["record_pivot"]["exactly_one_of"] == [
        "invalidates_node_id",
        "invalidates_claim_id",
    ]
    assert "exactly_one_of" not in semantic["record_dead_end"]
    assert semantic["abandon_question"] == {
        "op": "update",
        "entity_type": "question",
        "required_payload": {"status": "abandoned", "terminal_reason": "non-empty string"},
    }
    assert semantic["merge_questions"]["required_fields"] == ["replacement", "reason"]
    assert semantic["merge_questions"]["replacement_required_fields"] == [
        "text",
        "question_type",
        "status",
    ]
    assert semantic["merge_questions"]["controlled_values"] == {
        "replacement.status": ["staged", "active"]
    }
    assert semantic["retire_note"] == {
        "op": "update",
        "entity_type": "note",
        "required_fields": ["reason"],
        "controlled_values": {"reason": ["superseded", "reviewed_not_relevant"]},
    }
    # The validator and the contract name the same retirement reasons.
    assert {reason.value for reason in RETIRE_NOTE_REASONS} == set(RETIRE_NOTE_REASON_VALUES)
    assert any("semantic_operations entries override" in rule for rule in contract["rules"])


def test_graph_patch_response_schema_entity_types_track_draft_entity_tuple() -> None:
    operation_schema = graph_patch_response_schema()["properties"]["operations"]["items"]

    assert operation_schema["properties"]["entity_type"]["enum"] == list(
        _GRAPH_DRAFT_ENTITY_TYPES
    )
    assert "exploration_node" in _GRAPH_DRAFT_ENTITY_TYPES
    assert operation_schema["properties"]["semantic_type"]["enum"] == SEMANTIC_TYPES


def test_instructions_explain_negative_knowledge_labels() -> None:
    instructions = _instructions()
    prose = instructions.split("</trusted_api_payload_contract>", 1)[1]

    for label in NEGATIVE_KNOWLEDGE_LABELS:
        assert label in prose
    dead_end_guidance = prose.split("record_dead_end", 1)[1].split("record_decision", 1)[0]
    assert "failure_mode" in dead_end_guidance
    assert "lesson" in dead_end_guidance
    assert "exploration_node entities" in instructions
    assert "never use these labels to delete or hide information" in prose.lower()
    assert "exploration_node" in _analysis_instructions()
    # Prompt text changed without a version bump (binding decision for this wave).
    assert PROMPT_VERSION == "multimodal-graph-draft-v4"
    assert ANALYSIS_PROMPT_VERSION == "analysis-graph-draft-v4"


def test_batch_instructions_carry_review_memory_guidance() -> None:
    instructions = _batch_instructions()
    pending_sentence = (
        "say so in rationale and cite that pending change_set_id instead of creating a "
        "parallel entity"
    )
    rejection_sentence = (
        "If you re-propose something equivalent, state the new evidence in rationale"
    )

    assert "review_memory.pending_proposals" in instructions
    assert pending_sentence in instructions
    assert "review_memory.recent_rejections" in instructions
    assert rejection_sentence in instructions
    assert BATCH_PROMPT_VERSION == "daily-batch-graph-draft-v7"

    batch_context = {
        "batch_notes": [{"note_id": "source-1", "raw_content": "Observed result"}],
        "review_memory": {
            "reviewer_scoped": True,
            "pending_proposals": [],
            "recent_rejections": [],
        },
    }
    prompt = _batch_prompt_text(batch_context=batch_context, user_hint=None)
    trusted_prefix, untrusted_tail = prompt.split("<untrusted_batch_context>\n", 1)
    untrusted_payload, _ = untrusted_tail.split("\n</untrusted_batch_context>", 1)
    # The guidance is trusted system text, never part of the fenced packet.
    assert pending_sentence not in untrusted_payload
    assert rejection_sentence not in untrusted_payload
    assert pending_sentence not in trusted_prefix
    assert json.loads(untrusted_payload)["review_memory"] == batch_context["review_memory"]


def _operation(
    *,
    op: GraphChangeOp,
    entity_type: EntityType,
    semantic_type: GraphDraftSemanticType | None,
    payload: dict[str, Any],
    target_entity_id: UUID | None = None,
) -> GraphChangeOperation:
    return GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=1,
        op=op,
        entity_type=entity_type,
        semantic_type=semantic_type,
        payload=payload,
        target_entity_id=target_entity_id,
    )


def _recording_validator(
    seen: list[tuple[EntityType, UUID]],
) -> GraphPatchValidator:
    def get_entity(entity_type: EntityType, entity_id: UUID) -> Any:
        seen.append((entity_type, entity_id))
        return SimpleNamespace(entity_type=entity_type, entity_id=entity_id)

    return GraphPatchValidator(get_graph_entity=get_entity)


def _exploration_payload(
    project_id: UUID,
    question_id: UUID,
    node_type: str,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "project_id": str(project_id),
        "node_type": node_type,
        "title": "Bootstrap path underpowered",
        "target": {"entity_type": "question", "entity_id": str(question_id)},
        **fields,
    }


def _dead_end_fields() -> dict[str, Any]:
    return {
        "hypothesis": "Bootstrap intervals would separate the groups.",
        "failure_mode": "Intervals overlapped at every sample size.",
        "lesson": "Paired designs need a paired test.",
    }


def test_validator_accepts_record_dead_end_exploration_node() -> None:
    project_id, question_id = uuid4(), uuid4()
    seen: list[tuple[EntityType, UUID]] = []
    validator = _recording_validator(seen)
    operation = _operation(
        op=GraphChangeOp.CREATE,
        entity_type=EntityType.EXPLORATION_NODE,
        semantic_type=GraphDraftSemanticType.RECORD_DEAD_END,
        payload=_exploration_payload(project_id, question_id, "dead_end", **_dead_end_fields()),
    )

    validator.validate_operation(operation, operation.payload)

    assert (EntityType.PROJECT, project_id) in seen
    assert (EntityType.QUESTION, question_id) in seen


def test_validator_rejects_record_labels_with_mismatched_node_type() -> None:
    project_id, question_id = uuid4(), uuid4()
    validator = _recording_validator([])
    operation = _operation(
        op=GraphChangeOp.CREATE,
        entity_type=EntityType.EXPLORATION_NODE,
        semantic_type=GraphDraftSemanticType.RECORD_DEAD_END,
        payload=_exploration_payload(
            project_id,
            question_id,
            "decision",
            choice="Paired bootstrap",
            alternatives_considered=["Mixed model"],
            rationale="Matches the design.",
        ),
    )

    with pytest.raises(ValidationError, match="record_dead_end requires node_type dead_end"):
        validator.validate_operation(operation, operation.payload)


@pytest.mark.parametrize(
    ("node_type", "fields", "missing"),
    [
        (
            "decision",
            {"choice": "Paired bootstrap", "rationale": "Matches the design."},
            "alternatives_considered",
        ),
        (
            "dead_end",
            {"hypothesis": "Intervals separate groups.", "failure_mode": "They overlapped."},
            "lesson",
        ),
        (
            "pivot",
            {"rationale": "Switch designs.", "invalidates_claim_id": str(uuid4())},
            "trigger",
        ),
    ],
)
def test_validator_rejects_exploration_node_missing_per_type_fields(
    node_type: str,
    fields: dict[str, Any],
    missing: str,
) -> None:
    validator = _recording_validator([])
    operation = _operation(
        op=GraphChangeOp.CREATE,
        entity_type=EntityType.EXPLORATION_NODE,
        semantic_type=GraphDraftSemanticType.CREATE_ENTITY,
        payload=_exploration_payload(uuid4(), uuid4(), node_type, **fields),
    )

    with pytest.raises(ValidationError, match=f"node_type {node_type} requires {missing}"):
        validator.validate_operation(operation, operation.payload)


def test_validator_rejects_pivot_without_exactly_one_invalidation() -> None:
    validator = _recording_validator([])
    base = {"trigger": "Bootstrap failed.", "rationale": "Mixed model fits the design."}

    neither = _operation(
        op=GraphChangeOp.CREATE,
        entity_type=EntityType.EXPLORATION_NODE,
        semantic_type=GraphDraftSemanticType.RECORD_PIVOT,
        payload=_exploration_payload(uuid4(), uuid4(), "pivot", **base),
    )
    with pytest.raises(ValidationError, match="exactly one of invalidates_node_id"):
        validator.validate_operation(neither, neither.payload)

    both = _operation(
        op=GraphChangeOp.CREATE,
        entity_type=EntityType.EXPLORATION_NODE,
        semantic_type=GraphDraftSemanticType.RECORD_PIVOT,
        payload=_exploration_payload(
            uuid4(),
            uuid4(),
            "pivot",
            invalidates_node_id=str(uuid4()),
            invalidates_claim_id=str(uuid4()),
            **base,
        ),
    )
    with pytest.raises(ValidationError, match="exactly one of invalidates_node_id"):
        validator.validate_operation(both, both.payload)

    dead_end_with_invalidation = _operation(
        op=GraphChangeOp.CREATE,
        entity_type=EntityType.EXPLORATION_NODE,
        semantic_type=GraphDraftSemanticType.RECORD_DEAD_END,
        payload=_exploration_payload(
            uuid4(),
            uuid4(),
            "dead_end",
            invalidates_claim_id=str(uuid4()),
            **_dead_end_fields(),
        ),
    )
    with pytest.raises(ValidationError, match="Only pivot exploration nodes"):
        validator.validate_operation(
            dead_end_with_invalidation, dead_end_with_invalidation.payload
        )


def test_validator_checks_exploration_reference_fields_exist() -> None:
    parent_id, claim_id = uuid4(), uuid4()
    seen: list[tuple[EntityType, UUID]] = []
    validator = _recording_validator(seen)
    operation = _operation(
        op=GraphChangeOp.CREATE,
        entity_type=EntityType.EXPLORATION_NODE,
        semantic_type=GraphDraftSemanticType.RECORD_PIVOT,
        payload=_exploration_payload(
            uuid4(),
            uuid4(),
            "pivot",
            trigger="Bootstrap failed.",
            rationale="Mixed model fits the design.",
            invalidates_claim_id=str(claim_id),
            parent_node_ids=[str(parent_id)],
        ),
    )

    validator.validate_operation(operation, operation.payload)

    assert (EntityType.EXPLORATION_NODE, parent_id) in seen
    assert (EntityType.CLAIM, claim_id) in seen

    def missing_entity(entity_type: EntityType, entity_id: UUID) -> Any:
        if entity_type == EntityType.EXPLORATION_NODE:
            raise NotFoundError("Exploration node does not exist.")
        return SimpleNamespace()

    strict = GraphPatchValidator(get_graph_entity=missing_entity)
    with pytest.raises(ValidationError, match=f"unknown exploration_node ID: {parent_id}"):
        strict.validate_operation(operation, operation.payload)


def test_validator_abandon_question_requires_abandoned_status_and_terminal_reason() -> None:
    validator = _recording_validator([])
    question_id = uuid4()

    def abandon(payload: dict[str, Any]) -> GraphChangeOperation:
        return _operation(
            op=GraphChangeOp.UPDATE,
            entity_type=EntityType.QUESTION,
            semantic_type=GraphDraftSemanticType.ABANDON_QUESTION,
            payload=payload,
            target_entity_id=question_id,
        )

    wrong_status = abandon({"status": "active", "terminal_reason": "Out of scope."})
    with pytest.raises(ValidationError, match="requires status abandoned"):
        validator.validate_operation(wrong_status, wrong_status.payload)

    no_reason = abandon({"status": "abandoned"})
    with pytest.raises(ValidationError, match="requires a terminal_reason"):
        validator.validate_operation(no_reason, no_reason.payload)

    valid = abandon({"status": "abandoned", "terminal_reason": "Out of scope."})
    validator.validate_operation(valid, valid.payload)


def test_validator_merge_questions_uses_refactor_request_schema() -> None:
    seen: list[tuple[EntityType, UUID]] = []
    validator = _recording_validator(seen)
    source_id, child_id = uuid4(), uuid4()

    def merge(payload: dict[str, Any]) -> GraphChangeOperation:
        return _operation(
            op=GraphChangeOp.UPDATE,
            entity_type=EntityType.QUESTION,
            semantic_type=GraphDraftSemanticType.MERGE_QUESTIONS,
            payload=payload,
            target_entity_id=source_id,
        )

    question_update_shaped = merge({"text": "Merged question text", "status": "active"})
    with pytest.raises(ValidationError, match="failed API validation"):
        validator.validate_operation(question_update_shaped, question_update_shaped.payload)

    valid = merge(
        {
            "replacement": {
                "text": "Which contrast is testable this week?",
                "question_type": "hypothesis_driven",
                "status": "active",
            },
            "reason": "Two captures ask the same question.",
            "child_question_ids_to_reparent": [str(child_id)],
        }
    )
    validator.validate_operation(valid, valid.payload)
    assert (EntityType.QUESTION, source_id) in seen
    assert (EntityType.QUESTION, child_id) in seen


def test_validator_retire_note_reason_is_restricted() -> None:
    validator = _recording_validator([])
    note_id = uuid4()

    def retire(reason: str) -> GraphChangeOperation:
        return _operation(
            op=GraphChangeOp.UPDATE,
            entity_type=EntityType.NOTE,
            semantic_type=GraphDraftSemanticType.RETIRE_NOTE,
            payload={"reason": reason},
            target_entity_id=note_id,
        )

    unreviewed = retire("archived_unreviewed")
    with pytest.raises(ValidationError, match="superseded or reviewed_not_relevant"):
        validator.validate_operation(unreviewed, unreviewed.payload)

    superseded = retire("superseded")
    validator.validate_operation(superseded, superseded.payload)


def test_prompt_instructions_and_response_schema_include_resolve_prediction() -> None:
    operation_schema = graph_patch_response_schema()["properties"]["operations"]["items"]
    assert "resolve_prediction" in operation_schema["properties"]["semantic_type"]["enum"]
    assert [member.value for member in GraphDraftSemanticType] == SEMANTIC_TYPES
    for instructions in (_instructions(), _batch_instructions(), _analysis_instructions()):
        prose = instructions.split("</trusted_api_payload_contract>", 1)[1]
        assert "resolve_prediction" in prose
        assert "open_predictions" in prose
        assert "terminal_reason required" in prose
        assert "request_clarification" in prose
    # The analysis prompt no longer tells the model claims have no narrower label.
    no_narrower = _analysis_instructions().split("narrower semantic_type label", 1)[0]
    assert "analysis, and visualization there is no" in no_narrower
    assert "claim, and visualization" not in no_narrower
    contract = graph_draft_payload_contract()["semantic_operations"]["resolve_prediction"]
    assert contract["op"] == "update"
    assert contract["entity_type"] == "claim"
    assert contract["controlled_values"] == {"status": ["supported", "rejected"]}
    assert contract["when_rejected_required_fields"] == ["terminal_reason"]
    # Prompt text only; versions are pinned by the binding decision for this wave.
    assert PROMPT_VERSION == "multimodal-graph-draft-v4"
    assert BATCH_PROMPT_VERSION == "daily-batch-graph-draft-v7"
    assert ANALYSIS_PROMPT_VERSION == "analysis-graph-draft-v4"


def _resolve_prediction(payload: dict[str, Any], **overrides: Any) -> GraphChangeOperation:
    return _operation(
        op=overrides.get("op", GraphChangeOp.UPDATE),
        entity_type=overrides.get("entity_type", EntityType.CLAIM),
        semantic_type=GraphDraftSemanticType.RESOLVE_PREDICTION,
        payload=payload,
        target_entity_id=overrides.get("target_entity_id", uuid4()),
    )


def test_graph_patch_validator_accepts_resolve_prediction_update_on_claim() -> None:
    seen: list[tuple[EntityType, UUID]] = []
    validator = _recording_validator(seen)
    claim_id, dataset_id = uuid4(), uuid4()

    supported = _resolve_prediction(
        {"status": "supported", "supported_by_dataset_ids": [str(dataset_id)]},
        target_entity_id=claim_id,
    )
    validator.validate_operation(supported, supported.payload)
    assert (EntityType.CLAIM, claim_id) in seen
    assert (EntityType.DATASET, dataset_id) in seen

    rejected = _resolve_prediction(
        {"status": "rejected", "terminal_reason": "The committed dataset showed no effect."},
        target_entity_id=claim_id,
    )
    validator.validate_operation(rejected, rejected.payload)


def test_graph_patch_validator_rejects_resolve_prediction_without_terminal_status_or_reason():
    validator = _recording_validator([])

    missing = _resolve_prediction({"confidence": 90})
    with pytest.raises(ValidationError, match="status to supported or rejected"):
        validator.validate_operation(missing, missing.payload)

    still_testing = _resolve_prediction({"status": "testing"})
    with pytest.raises(ValidationError, match="status to supported or rejected"):
        validator.validate_operation(still_testing, still_testing.payload)

    no_reason = _resolve_prediction({"status": "rejected"})
    with pytest.raises(ValidationError, match="rejected requires terminal_reason"):
        validator.validate_operation(no_reason, no_reason.payload)

    blank_reason = _resolve_prediction({"status": "rejected", "terminal_reason": "   "})
    with pytest.raises(ValidationError):
        validator.validate_operation(blank_reason, blank_reason.payload)


def test_graph_patch_validator_rejects_resolve_prediction_on_non_claim_target() -> None:
    validator = _recording_validator([])
    on_question = _resolve_prediction(
        {"status": "abandoned", "terminal_reason": "x"},
        entity_type=EntityType.QUESTION,
    )
    with pytest.raises(ValidationError, match="cannot be used with update question"):
        validator.validate_operation(on_question, on_question.payload)
    as_create = _resolve_prediction(
        {"project_id": str(uuid4()), "statement": "New", "confidence": 50},
        op=GraphChangeOp.CREATE,
    )
    with pytest.raises(ValidationError, match="cannot be used with create claim"):
        validator.validate_operation(as_create, as_create.payload)


def test_graph_patch_applier_forwards_resolve_prediction_to_claim_update() -> None:
    change_set = _change_set(uuid4())
    claim_id, dataset_id = uuid4(), uuid4()
    captured: dict[str, Any] = {}

    def update_claim(target_id: UUID, **kwargs: Any) -> Any:
        captured.update({"claim_id": target_id, **kwargs})
        return SimpleNamespace(claim_id=target_id)

    applier = _exploration_applier({}, claims=SimpleNamespace(update_claim=update_claim))
    operation = _resolve_prediction(
        {
            "status": "supported",
            "supported_by_dataset_ids": [str(dataset_id)],
            "terminal_reason": None,
        },
        target_entity_id=claim_id,
    )

    result = applier.apply_graph_operation(operation, ref_map={}, actor=None, change_set=change_set)

    assert result.claim_id == claim_id
    assert captured["claim_id"] == claim_id
    assert captured["status"].value == "supported"
    assert captured["supported_by_dataset_ids"] == [dataset_id]
    assert captured["terminal_reason"] is None
    assert captured["change_set_id"] == change_set.change_set_id
    assert captured["origin"] == EntityOrigin.AI_SUGGESTED


def test_semantic_allowed_targets_cover_every_new_label() -> None:
    generic = {GraphDraftSemanticType.CREATE_ENTITY, GraphDraftSemanticType.UPDATE_ENTITY}
    assert set(_SEMANTIC_ALLOWED_TARGETS) == set(GraphDraftSemanticType) - generic
    for label in (
        GraphDraftSemanticType.RECORD_DECISION,
        GraphDraftSemanticType.RECORD_DEAD_END,
        GraphDraftSemanticType.RECORD_PIVOT,
    ):
        assert _SEMANTIC_ALLOWED_TARGETS[label] == {
            (GraphChangeOp.CREATE, EntityType.EXPLORATION_NODE)
        }
    assert _SEMANTIC_ALLOWED_TARGETS[GraphDraftSemanticType.ABANDON_QUESTION] == {
        (GraphChangeOp.UPDATE, EntityType.QUESTION)
    }
    assert _SEMANTIC_ALLOWED_TARGETS[GraphDraftSemanticType.MERGE_QUESTIONS] == {
        (GraphChangeOp.UPDATE, EntityType.QUESTION)
    }
    assert _SEMANTIC_ALLOWED_TARGETS[GraphDraftSemanticType.RETIRE_NOTE] == {
        (GraphChangeOp.UPDATE, EntityType.NOTE)
    }


_ENTITY_GETTERS = {
    "projects": "get_project",
    "questions": "get_question",
    "notes": "get_note",
    "sessions": "get_session",
    "datasets": "get_dataset",
    "analyses": "get_analysis",
    "claims": "get_claim",
    "visualizations": "get_visualization",
}


def _missing_entity(entity_id: UUID) -> Any:
    raise NotFoundError(f"{entity_id} does not exist.")


def _stub_builder(**overrides: Any) -> GraphContextBuilder:
    services: dict[str, Any] = {
        name: SimpleNamespace(**{getter: _missing_entity})
        for name, getter in _ENTITY_GETTERS.items()
    }
    services.update(overrides)
    return GraphContextBuilder(**services)


def _exploration_node(project_id: UUID) -> ExplorationNode:
    return ExplorationNode(
        node_id=uuid4(),
        project_id=project_id,
        node_type=ExplorationNodeType.DEAD_END,
        title="Bootstrap path underpowered",
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=uuid4()),
        hypothesis="h",
        failure_mode="f",
        lesson="l",
    )


def test_graph_context_builder_resolves_exploration_nodes() -> None:
    node = _exploration_node(uuid4())
    builder = _stub_builder(
        exploration=SimpleNamespace(get_exploration_node=lambda node_id: node),
    )

    resolved = builder.get_graph_entity(EntityType.EXPLORATION_NODE, node.node_id)

    assert resolved is node
    assert entity_id(EntityType.EXPLORATION_NODE, node) == node.node_id
    assert _entity_label(EntityType.EXPLORATION_NODE, node) == node.title


def test_graph_context_builder_without_exploration_service_rejects_exploration_refs() -> None:
    builder = _stub_builder(exploration=None)

    with pytest.raises(ValidationError, match="Unsupported entity type."):
        builder.get_graph_entity(EntityType.EXPLORATION_NODE, uuid4())


def _review_change_set(
    project_id: UUID,
    *,
    status: GraphChangeSetStatus,
    review_assignee_user_id: UUID | None = None,
    created_by_user_id: UUID | None = None,
    created_at_offset: timedelta = timedelta(),
    reviewed_by: str | None = None,
    reviewed_at_offset: timedelta | None = None,
    review_note: str | None = None,
) -> GraphChangeSet:
    now = utc_now()
    return GraphChangeSet(
        change_set_id=uuid4(),
        project_id=project_id,
        source_note_id=uuid4(),
        model="fake-gpt",
        prompt_version="test",
        status=status,
        review_assignee_user_id=review_assignee_user_id,
        created_by_user_id=created_by_user_id,
        created_at=now - created_at_offset,
        reviewed_by=reviewed_by,
        reviewed_at=(now - reviewed_at_offset) if reviewed_at_offset is not None else None,
        review_note=review_note,
    )


def _add_operation(
    change_set: GraphChangeSet,
    *,
    op: GraphChangeOp = GraphChangeOp.CREATE,
    entity_type: EntityType = EntityType.QUESTION,
    semantic_type: GraphDraftSemanticType = GraphDraftSemanticType.SUGGEST_NEW_QUESTION,
    payload: dict[str, Any] | None = None,
    target_entity_id: UUID | None = None,
    status: GraphChangeOperationStatus = GraphChangeOperationStatus.PROPOSED,
    review_note: str | None = None,
    rejected_by: str | None = None,
    rejected_ago: timedelta | None = None,
) -> GraphChangeOperation:
    error_metadata: dict[str, Any] = {}
    if rejected_by is not None:
        error_metadata = {
            REVIEWED_BY_KEY: rejected_by,
            REVIEWED_AT_KEY: (utc_now() - (rejected_ago or timedelta())).isoformat(),
            REVIEW_NOTE_KEY: review_note,
        }
    operation = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=change_set.change_set_id,
        sequence=len(change_set.operations) + 1,
        op=op,
        entity_type=entity_type,
        semantic_type=semantic_type,
        payload=payload if payload is not None else {"text": "Does pooling change the result?"},
        target_entity_id=target_entity_id,
        status=status,
        review_note=review_note,
        error_metadata=error_metadata,
    )
    change_set.operations.append(operation)
    return operation


def _review_memory_builder(change_sets: list[GraphChangeSet]) -> GraphContextBuilder:
    def get_question(question_id: UUID) -> Question:
        return Question(
            question_id=question_id,
            project_id=uuid4(),
            text=f"Existing question {question_id}",
            question_type=QuestionType.DESCRIPTIVE,
        )

    return _stub_builder(
        questions=SimpleNamespace(get_question=get_question),
        review_memory=SimpleNamespace(
            list_review_memory_change_sets=lambda project_id, *, statuses, limit: [
                item for item in change_sets if item.status in statuses
            ][:limit]
        ),
    )


def test_batch_packet_review_memory_lists_reviewer_pending_proposals() -> None:
    project_id, reviewer, other = uuid4(), uuid4(), uuid4()
    existing_question_id = uuid4()
    assigned = _review_change_set(
        project_id,
        status=GraphChangeSetStatus.READY,
        review_assignee_user_id=reviewer,
        created_at_offset=timedelta(hours=2),
    )
    _add_operation(assigned, payload={"text": "Do pooled notes support a merged observation?"})
    _add_operation(
        assigned,
        op=GraphChangeOp.UPDATE,
        semantic_type=GraphDraftSemanticType.UPDATE_ENTITY,
        payload={"status": "active"},
        target_entity_id=existing_question_id,
    )
    _add_operation(assigned, status=GraphChangeOperationStatus.REJECTED)
    authored_unassigned = _review_change_set(
        project_id,
        status=GraphChangeSetStatus.SUBMITTED,
        created_by_user_id=reviewer,
        created_at_offset=timedelta(hours=1),
    )
    _add_operation(authored_unassigned, payload={"text": "Is lane 2 the same gel?"})
    someone_elses = _review_change_set(
        project_id, status=GraphChangeSetStatus.READY, review_assignee_user_id=other
    )
    _add_operation(someone_elses, payload={"text": "Not this reviewer's proposal"})
    already_committed = _review_change_set(
        project_id, status=GraphChangeSetStatus.COMMITTED, review_assignee_user_id=reviewer
    )
    _add_operation(already_committed, payload={"text": "Committed already"})
    builder = _review_memory_builder(
        [assigned, authored_unassigned, someone_elses, already_committed]
    )

    memory = builder.build_review_memory(
        project_ids={project_id},
        context_owner=BatchReviewer(reviewer=str(reviewer), reviewer_user_id=reviewer),
        now=utc_now(),
    )

    assert memory["reviewer_scoped"] is True
    assert memory["reviewer_user_id"] == str(reviewer)
    assert memory["pending_proposals_truncated"] is False
    # Newest change set first; the rejected operation and other reviewers' sets are absent.
    assert [item["change_set_id"] for item in memory["pending_proposals"]] == [
        str(authored_unassigned.change_set_id),
        str(assigned.change_set_id),
        str(assigned.change_set_id),
    ]
    assert memory["pending_proposals"][0] == {
        "change_set_id": str(authored_unassigned.change_set_id),
        "change_set_status": "submitted",
        "semantic_type": "suggest_new_question",
        "op": "create",
        "entity_type": "question",
        "target": "Is lane 2 the same gel?",
    }
    assert memory["pending_proposals"][2]["target"] == f"Existing question {existing_question_id}"
    assert memory["recent_rejections"] == []

    summary = _graph_batch_context_summary(
        {"batch_notes": [], "source_artifacts": [], "projects": [], "review_memory": memory}
    )
    assert summary["counts"]["pending_proposals"] == 3
    assert summary["counts"]["recent_rejections"] == 0
    assert summary["review_memory"] == {
        "reviewer_scoped": True,
        "pending_proposals": 3,
        "recent_rejections": 0,
        "pending_proposals_truncated": False,
    }
    assert REVIEW_MEMORY_NOT_SCOPED_WARNING not in summary["warnings"]


def test_batch_packet_review_memory_caps_pending_items_and_chars() -> None:
    project_id, reviewer = uuid4(), uuid4()
    owner = BatchReviewer(reviewer=str(reviewer), reviewer_user_id=reviewer)

    many = _review_change_set(
        project_id, status=GraphChangeSetStatus.READY, review_assignee_user_id=reviewer
    )
    for index in range(PENDING_PROPOSALS_ITEM_LIMIT + 1):
        _add_operation(many, payload={"text": f"Proposal {index}"})
    capped = _review_memory_builder([many]).build_review_memory(
        project_ids={project_id}, context_owner=owner, now=utc_now()
    )
    assert len(capped["pending_proposals"]) == PENDING_PROPOSALS_ITEM_LIMIT
    assert capped["pending_proposals_truncated"] is True

    oversized = _review_change_set(
        project_id, status=GraphChangeSetStatus.READY, review_assignee_user_id=reviewer
    )
    for index in range(PENDING_PROPOSALS_ITEM_LIMIT):
        _add_operation(oversized, payload={"text": f"{index}:" + ("x" * 600)})
    budgeted = _review_memory_builder([oversized]).build_review_memory(
        project_ids={project_id}, context_owner=owner, now=utc_now()
    )
    items = budgeted["pending_proposals"]
    assert 0 < len(items) < PENDING_PROPOSALS_ITEM_LIMIT
    assert budgeted["pending_proposals_truncated"] is True
    assert len(json.dumps(items, sort_keys=True)) <= PENDING_PROPOSALS_CHAR_BUDGET
    assert all(len(item["target"]) <= REVIEW_MEMORY_NOTE_MAX_CHARS for item in items)


def test_batch_packet_review_memory_recent_rejections_are_reviewer_and_window_scoped() -> None:
    project_id, reviewer, other = uuid4(), uuid4(), uuid4()
    owner = BatchReviewer(reviewer=str(reviewer), reviewer_user_id=reviewer)

    per_operation = _review_change_set(
        project_id, status=GraphChangeSetStatus.COMMITTED, review_assignee_user_id=reviewer
    )
    recent = _add_operation(
        per_operation,
        payload={"text": "Rejected three days ago"},
        status=GraphChangeOperationStatus.REJECTED,
        review_note="Not a question",
        rejected_by=str(reviewer),
        rejected_ago=timedelta(days=3),
    )
    _add_operation(
        per_operation,
        payload={"text": "Rejected twenty days ago"},
        status=GraphChangeOperationStatus.REJECTED,
        review_note="Too old to matter",
        rejected_by=str(reviewer),
        rejected_ago=timedelta(days=20),
    )
    _add_operation(
        per_operation,
        payload={"text": "Rejected by someone else"},
        status=GraphChangeOperationStatus.REJECTED,
        review_note="Other reviewer",
        rejected_by=str(other),
        rejected_ago=timedelta(days=1),
    )
    _add_operation(
        per_operation,
        payload={"text": "Rejected without a note"},
        status=GraphChangeOperationStatus.REJECTED,
        rejected_by=str(reviewer),
        rejected_ago=timedelta(days=1),
    )
    whole_set = _review_change_set(
        project_id,
        status=GraphChangeSetStatus.REJECTED,
        review_assignee_user_id=reviewer,
        reviewed_by=str(reviewer),
        reviewed_at_offset=timedelta(days=2),
        review_note="n" * 300,
    )
    first_of_set = _add_operation(whole_set, payload={"text": "Whole set, op one"})
    second_of_set = _add_operation(whole_set, payload={"text": "Whole set, op two"})
    builder = _review_memory_builder([per_operation, whole_set])

    memory = builder.build_review_memory(
        project_ids={project_id}, context_owner=owner, now=utc_now()
    )

    rejections = memory["recent_rejections"]
    assert [item["operation_id"] for item in rejections] == sorted(
        [str(first_of_set.operation_id), str(second_of_set.operation_id)]
    ) + [str(recent.operation_id)]
    assert rejections[0]["note"] == "n" * REVIEW_MEMORY_NOTE_MAX_CHARS
    assert rejections[0]["change_set_id"] == str(whole_set.change_set_id)
    assert rejections[-1] == {
        "change_set_id": str(per_operation.change_set_id),
        "operation_id": str(recent.operation_id),
        "semantic_type": "suggest_new_question",
        "op": "create",
        "entity_type": "question",
        "target": "Rejected three days ago",
        "note": "Not a question",
        "rejected_at": recent.error_metadata[REVIEWED_AT_KEY],
    }

    flood = _review_change_set(
        project_id,
        status=GraphChangeSetStatus.REJECTED,
        review_assignee_user_id=reviewer,
        reviewed_by=str(reviewer),
        reviewed_at_offset=timedelta(hours=1),
        review_note="Newest rejection",
    )
    for index in range(RECENT_REJECTIONS_ITEM_LIMIT + 5):
        _add_operation(flood, payload={"text": f"Flood {index}"})
    capped = _review_memory_builder([per_operation, whole_set, flood]).build_review_memory(
        project_ids={project_id}, context_owner=owner, now=utc_now()
    )
    assert len(capped["recent_rejections"]) == RECENT_REJECTIONS_ITEM_LIMIT
    assert {item["change_set_id"] for item in capped["recent_rejections"]} == {
        str(flood.change_set_id)
    }


def test_batch_packet_review_memory_without_reviewer_is_empty_and_flagged() -> None:
    project_id = uuid4()
    change_set = _review_change_set(
        project_id, status=GraphChangeSetStatus.READY, review_assignee_user_id=uuid4()
    )
    _add_operation(change_set)
    builder = _review_memory_builder([change_set])
    empty = {
        "reviewer_scoped": False,
        "reviewer_user_id": None,
        "pending_proposals": [],
        "pending_proposals_truncated": False,
        "recent_rejections": [],
    }

    assert (
        builder.build_review_memory(project_ids={project_id}, context_owner=None, now=utc_now())
        == empty
    )
    legacy = BatchReviewer(reviewer="legacy-reviewer", reviewer_user_id=None)
    assert (
        builder.build_review_memory(project_ids={project_id}, context_owner=legacy, now=utc_now())
        == empty
    )
    without_records = _stub_builder(review_memory=None)
    assert (
        without_records.build_review_memory(
            project_ids={project_id},
            context_owner=BatchReviewer(reviewer="u", reviewer_user_id=uuid4()),
            now=utc_now(),
        )
        == empty
    )

    summary = _graph_batch_context_summary(
        {"batch_notes": [], "source_artifacts": [], "projects": [], "review_memory": empty}
    )
    assert REVIEW_MEMORY_NOT_SCOPED_WARNING in summary["warnings"]
    assert summary["review_memory"]["reviewer_scoped"] is False
    # Packets from before review memory existed summarize the same way.
    legacy_summary = _graph_batch_context_summary(
        {"batch_notes": [], "source_artifacts": [], "projects": []}
    )
    assert REVIEW_MEMORY_NOT_SCOPED_WARNING in legacy_summary["warnings"]
    assert legacy_summary["counts"]["pending_proposals"] == 0


def test_context_owner_for_prefers_assignee_then_actor() -> None:
    assignee_id, actor_id = uuid4(), uuid4()
    actor = SimpleNamespace(user_id=actor_id)

    assert context_owner_for("legacy", assignee_id, actor) == BatchReviewer(
        reviewer="legacy", reviewer_user_id=assignee_id
    )
    assert context_owner_for(None, assignee_id, actor) == BatchReviewer(
        reviewer=str(assignee_id), reviewer_user_id=assignee_id
    )
    assert context_owner_for("legacy", None, actor) == BatchReviewer(
        reviewer="legacy", reviewer_user_id=None
    )
    assert context_owner_for(None, None, actor) == BatchReviewer(
        reviewer=str(actor_id), reviewer_user_id=actor_id
    )
    assert context_owner_for(None, None, None) is None


def test_compose_revise_hint_includes_operation_review_notes() -> None:
    change_set = _change_set(uuid4())
    noted = _add_operation(
        change_set,
        status=GraphChangeOperationStatus.REJECTED,
        review_note="  Not a question  ",
        payload={"text": "Rig 2 Fly 12"},
    )
    silent = _add_operation(change_set, payload={"text": "Keep this one"})

    hint = compose_revise_hint(
        [noted, silent],
        "Drop the identifier.",
        attachment_labels=["whiteboard.png"],
    )

    assert hint.startswith(REVISION_HEADING + " You previously proposed")
    assert "[rejected] suggest_new_question on question" in hint
    assert "(reviewer note: Not a question)" in hint
    assert hint.count("(reviewer note:") == 1
    assert "Reviewer feedback (authoritative): Drop the identifier." in hint
    assert "whiteboard.png" in hint
    with pytest.raises(ValueError, match="Unknown revision hint heading"):
        compose_revise_hint([noted], "x", heading="SOMETHING ELSE.")


def test_compose_rejected_redraft_hint_uses_change_set_review_note_and_fences_operations() -> None:
    rejected = _review_change_set(
        uuid4(),
        status=GraphChangeSetStatus.REJECTED,
        reviewed_by="reviewer",
        reviewed_at_offset=timedelta(hours=1),
        review_note="Try a different framing.",
    )
    _add_operation(
        rejected,
        status=GraphChangeOperationStatus.REJECTED,
        review_note="Not a question",
        payload={"text": "Rig 2 Fly 12"},
    )

    hint = compose_rejected_redraft_hint(rejected, user_hint="focus on controls")

    assert hint.startswith("focus on controls\n\n" + REJECTED_REDRAFT_HEADING)
    assert "A prior draft for this note was rejected by its reviewer" in hint
    fenced = hint.split("<prior_proposed_operations>\n", 1)[1].split(
        "\n</prior_proposed_operations>", 1
    )[0]
    assert "[rejected] suggest_new_question on question" in fenced
    assert "(reviewer note: Not a question)" in fenced
    assert "Reviewer feedback (authoritative): Try a different framing." in hint

    rejected.review_note = "   "
    bare = compose_rejected_redraft_hint(rejected, user_hint=None)
    assert bare.startswith(REJECTED_REDRAFT_HEADING)
    assert "(no review note recorded)" in bare

    summary = prior_rejection_summary(rejected)
    assert summary["change_set_id"] == str(rejected.change_set_id)
    assert summary["reviewed_by"] == "reviewer"
    assert summary["reviewed_at"] == rejected.reviewed_at.isoformat()
    assert summary["rejected_operation_count"] == 1


def _exploration_applier(captured: dict[str, Any], **overrides: Any) -> GraphPatchApplier:
    def create_exploration_node(project_id: UUID, **kwargs: Any) -> ExplorationNode:
        captured.update({"project_id": project_id, **kwargs})
        return ExplorationNode(
            node_id=uuid4(),
            project_id=project_id,
            node_type=kwargs["node_type"],
            title=kwargs["title"],
            target=kwargs["target"],
            hypothesis=kwargs.get("hypothesis"),
            failure_mode=kwargs.get("failure_mode"),
            lesson=kwargs.get("lesson"),
            origin=kwargs["origin"],
            change_set_id=kwargs["change_set_id"],
        )

    services: dict[str, Any] = {
        name: SimpleNamespace()
        for name in (
            "projects",
            "questions",
            "notes",
            "sessions",
            "datasets",
            "analyses",
            "claims",
            "visualizations",
        )
    }
    services["exploration"] = SimpleNamespace(create_exploration_node=create_exploration_node)
    services.update(overrides)
    return GraphPatchApplier(**services)


def _record_dead_end_operation(
    change_set: GraphChangeSet,
    *,
    error_metadata: dict[str, Any] | None = None,
) -> GraphChangeOperation:
    return GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=change_set.change_set_id,
        sequence=1,
        op=GraphChangeOp.CREATE,
        entity_type=EntityType.EXPLORATION_NODE,
        semantic_type=GraphDraftSemanticType.RECORD_DEAD_END,
        payload={
            "project_id": str(change_set.project_id),
            "node_type": "dead_end",
            "title": "Bootstrap path underpowered",
            "target": {"entity_type": "question", "entity_id": {"$ref": "q1"}},
            **_dead_end_fields(),
        },
        error_metadata=error_metadata or {},
    )


def test_graph_patch_applier_creates_exploration_node_with_origin_and_change_set_backlink() -> None:
    change_set = _change_set(uuid4())
    question_id = uuid4()
    captured: dict[str, Any] = {}
    applier = _exploration_applier(captured)

    result = applier.apply_graph_operation(
        _record_dead_end_operation(change_set),
        ref_map={"q1": question_id},
        actor=None,
        change_set=change_set,
    )

    assert isinstance(result, ExplorationNode)
    assert result.node_type == ExplorationNodeType.DEAD_END
    assert captured["project_id"] == change_set.project_id
    assert captured["target"] == EntityRef(entity_type=EntityType.QUESTION, entity_id=question_id)
    assert captured["origin"] == EntityOrigin.AI_SUGGESTED
    assert captured["change_set_id"] == change_set.change_set_id
    assert captured["origin_prompt_version"] == change_set.prompt_version
    assert captured["origin_model"] == change_set.model
    assert captured["lesson"] == "Paired designs need a paired test."


def test_graph_patch_applier_stamps_user_revised_on_edited_exploration_node() -> None:
    change_set = _change_set(uuid4())
    captured: dict[str, Any] = {}
    applier = _exploration_applier(captured)

    applier.apply_graph_operation(
        _record_dead_end_operation(
            change_set, error_metadata={"edited_at": utc_now().isoformat()}
        ),
        ref_map={"q1": uuid4()},
        actor=None,
        change_set=change_set,
    )

    assert captured["origin"] == EntityOrigin.USER_REVISED


def test_graph_patch_applier_rejects_exploration_node_without_service() -> None:
    change_set = _change_set(uuid4())
    applier = _exploration_applier({}, exploration=None)

    with pytest.raises(ValidationError, match="Exploration service is not configured."):
        applier.apply_graph_operation(
            _record_dead_end_operation(change_set),
            ref_map={"q1": uuid4()},
            actor=None,
            change_set=change_set,
        )


def test_graph_patch_applier_merge_questions_calls_refactor_with_reason_and_origin() -> None:
    change_set = _change_set(uuid4())
    source_id = uuid4()
    captured: dict[str, Any] = {}
    replacement = Question(
        question_id=uuid4(),
        project_id=change_set.project_id,
        text="Which contrast is testable this week?",
        question_type=QuestionType.HYPOTHESIS_DRIVEN,
    )

    def refactor_question(question_id: UUID, **kwargs: Any) -> Any:
        captured.update({"question_id": question_id, **kwargs})
        return SimpleNamespace(replacement_question=replacement)

    applier = _exploration_applier(
        {}, questions=SimpleNamespace(refactor_question=refactor_question)
    )
    operation = _operation(
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.QUESTION,
        semantic_type=GraphDraftSemanticType.MERGE_QUESTIONS,
        payload={
            "replacement": {
                "text": replacement.text,
                "question_type": "hypothesis_driven",
                "status": "active",
            },
            "reason": "Two captures ask the same question.",
        },
        target_entity_id=source_id,
    )

    result = applier.apply_graph_operation(
        operation, ref_map={}, actor=None, change_set=change_set
    )

    assert result is replacement
    assert captured["question_id"] == source_id
    assert captured["replacement_text"] == replacement.text
    assert captured["replacement_status"].value == "active"
    assert captured["reason"] == "Two captures ask the same question."
    assert captured["origin"] == EntityOrigin.AI_SUGGESTED
    assert captured["change_set_id"] == change_set.change_set_id
    assert captured["origin_prompt_version"] == change_set.prompt_version


def test_graph_patch_applier_retire_note_calls_archive_note_with_reason() -> None:
    change_set = _change_set(uuid4())
    note_id = uuid4()
    captured: dict[str, Any] = {}

    def archive_note(target_id: UUID, *, reason: NoteArchiveReason, actor: Any) -> Note:
        captured.update({"note_id": target_id, "reason": reason, "actor": actor})
        return Note(note_id=target_id, project_id=change_set.project_id, raw_content="old")

    applier = _exploration_applier({}, notes=SimpleNamespace(archive_note=archive_note))

    def retire(reason: str) -> GraphChangeOperation:
        return _operation(
            op=GraphChangeOp.UPDATE,
            entity_type=EntityType.NOTE,
            semantic_type=GraphDraftSemanticType.RETIRE_NOTE,
            payload={"reason": reason},
            target_entity_id=note_id,
        )

    result = applier.apply_graph_operation(
        retire("superseded"), ref_map={}, actor=None, change_set=change_set
    )
    assert result.note_id == note_id
    assert captured["reason"] == NoteArchiveReason.SUPERSEDED

    with pytest.raises(ValidationError, match="superseded or reviewed_not_relevant"):
        applier.apply_graph_operation(
            retire("archived_unreviewed"), ref_map={}, actor=None, change_set=change_set
        )
