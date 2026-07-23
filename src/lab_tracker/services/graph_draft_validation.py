"""Graph draft patch parsing and validation."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError as PydanticValidationError

from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.goals_attributes import validate_goal_attributes
from lab_tracker.graph_drafting import GraphDraftingError
from lab_tracker.models import (
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeSet,
    GraphDraftSemanticType,
)
from lab_tracker.schemas import (
    AnalysisCreate,
    AnalysisUpdate,
    ClaimCreate,
    ClaimUpdate,
    DatasetCreate,
    DatasetUpdate,
    GoalCreate,
    GoalUpdate,
    NoteCreate,
    NoteUpdate,
    ProjectCreate,
    ProjectUpdate,
    QuestionCreate,
    QuestionUpdate,
    SessionCreate,
    SessionUpdate,
    VisualizationCreate,
    VisualizationUpdate,
)
from lab_tracker.services.graph_draft_context import EntityResult

_REF_VALIDATION_PLACEHOLDER = "00000000-0000-0000-0000-000000000001"
_CREATE_SCHEMAS = {
    EntityType.PROJECT: ProjectCreate,
    EntityType.QUESTION: QuestionCreate,
    EntityType.NOTE: NoteCreate,
    EntityType.SESSION: SessionCreate,
    EntityType.DATASET: DatasetCreate,
    EntityType.ANALYSIS: AnalysisCreate,
    EntityType.CLAIM: ClaimCreate,
    EntityType.VISUALIZATION: VisualizationCreate,
    EntityType.GOAL: GoalCreate,
}
_UPDATE_SCHEMAS = {
    EntityType.PROJECT: ProjectUpdate,
    EntityType.QUESTION: QuestionUpdate,
    EntityType.NOTE: NoteUpdate,
    EntityType.SESSION: SessionUpdate,
    EntityType.DATASET: DatasetUpdate,
    EntityType.ANALYSIS: AnalysisUpdate,
    EntityType.CLAIM: ClaimUpdate,
    EntityType.VISUALIZATION: VisualizationUpdate,
    EntityType.GOAL: GoalUpdate,
}
_SEMANTIC_ALLOWED_TARGETS = {
    GraphDraftSemanticType.CREATE_NOTE: {(GraphChangeOp.CREATE, EntityType.NOTE)},
    GraphDraftSemanticType.LINK_NOTE_TO_QUESTION: {(GraphChangeOp.UPDATE, EntityType.NOTE)},
    GraphDraftSemanticType.LINK_NOTE_TO_SESSION: {(GraphChangeOp.UPDATE, EntityType.NOTE)},
    GraphDraftSemanticType.LINK_NOTE_TO_DATASET: {(GraphChangeOp.UPDATE, EntityType.NOTE)},
    GraphDraftSemanticType.LINK_NOTE_TO_ANALYSIS: {(GraphChangeOp.UPDATE, EntityType.NOTE)},
    GraphDraftSemanticType.SUGGEST_NEW_QUESTION: {(GraphChangeOp.CREATE, EntityType.QUESTION)},
    GraphDraftSemanticType.SUGGEST_NEW_DATASET: {(GraphChangeOp.CREATE, EntityType.DATASET)},
    GraphDraftSemanticType.SUGGEST_NEW_GOAL: {(GraphChangeOp.CREATE, EntityType.GOAL)},
    GraphDraftSemanticType.LINK_NODE_TO_GOAL: {(GraphChangeOp.UPDATE, EntityType.GOAL)},
    GraphDraftSemanticType.UPDATE_GOAL: {(GraphChangeOp.UPDATE, EntityType.GOAL)},
    GraphDraftSemanticType.SUGGEST_FOLLOWUP: {
        (GraphChangeOp.CREATE, EntityType.NOTE),
        (GraphChangeOp.CREATE, EntityType.QUESTION),
    },
    GraphDraftSemanticType.REQUEST_CLARIFICATION: {
        (GraphChangeOp.CREATE, EntityType.NOTE),
        (GraphChangeOp.UPDATE, EntityType.NOTE),
    },
}
_ENTITY_ID_FIELDS = {
    "project_id": EntityType.PROJECT,
    "question_id": EntityType.QUESTION,
    "primary_question_id": EntityType.QUESTION,
    "linked_question_id": EntityType.QUESTION,
    "session_id": EntityType.SESSION,
    "source_session_id": EntityType.SESSION,
    "dataset_id": EntityType.DATASET,
    "analysis_id": EntityType.ANALYSIS,
    "note_id": EntityType.NOTE,
    "viz_id": EntityType.VISUALIZATION,
    "visualization_id": EntityType.VISUALIZATION,
    "claim_id": EntityType.CLAIM,
    "goal_id": EntityType.GOAL,
}
_ENTITY_ID_LIST_FIELDS = {
    "parent_question_ids": EntityType.QUESTION,
    "secondary_question_ids": EntityType.QUESTION,
    "dataset_ids": EntityType.DATASET,
    "supported_by_dataset_ids": EntityType.DATASET,
    "supported_by_analysis_ids": EntityType.ANALYSIS,
    "related_claim_ids": EntityType.CLAIM,
    "note_ids": EntityType.NOTE,
}
_SOURCE_NOTE_ID_KEYS = ("source_note_ids", "source_note_id", "note_id")
_SOURCE_NOTE_IDS_RESOLUTION_EXPLICIT = "explicit"
_SOURCE_NOTE_IDS_RESOLUTION_SINGLE_SOURCE_FALLBACK = "single_source_fallback"
_SOURCE_NOTE_IDS_RESOLUTION_AMBIGUOUS_BUNDLE = "ambiguous_bundle"


class GraphPatchValidator:
    def __init__(
        self,
        *,
        get_graph_entity: Callable[[EntityType, UUID], EntityResult],
    ) -> None:
        self._get_graph_entity = get_graph_entity

    def validate_top_level(self, graph_patch: dict[str, Any]) -> None:
        _validate_graph_patch_top_level(graph_patch)

    def validate_operation(
        self,
        operation: GraphChangeOperation,
        payload: dict[str, Any],
    ) -> None:
        _validate_graph_operation_payload(operation, payload)
        self.ensure_operation_references_exist(operation, payload)
        self._validate_goal_update_payload(operation, payload)
        _validate_semantic_operation_target(operation)

    def operations_from_graph_patch(
        self,
        change_set: GraphChangeSet,
        graph_patch: dict[str, Any],
    ) -> list[GraphChangeOperation]:
        if not isinstance(graph_patch, dict):
            raise GraphDraftingError("GPT graph patch was not an object.")
        raw_operations = graph_patch.get("operations")
        if not isinstance(raw_operations, list):
            raise GraphDraftingError("GPT graph patch did not include an operations list.")
        operations: list[GraphChangeOperation] = []
        for index, item in enumerate(raw_operations, start=1):
            if not isinstance(item, dict):
                raise GraphDraftingError("GPT graph patch operation was not an object.")
            _validate_graph_patch_operation_shape(item)
            payload = _payload_from_json(item.get("payload_json"))
            source_refs = _normalize_source_refs(
                item["source_refs"],
                change_set=change_set,
            )
            try:
                operation = GraphChangeOperation(
                    operation_id=uuid4(),
                    change_set_id=change_set.change_set_id,
                    sequence=index,
                    op=GraphChangeOp(item.get("op")),
                    entity_type=EntityType(item.get("entity_type")),
                    semantic_type=GraphDraftSemanticType(item.get("semantic_type")),
                    target_entity_id=item.get("target_entity_id"),
                    client_ref=item.get("client_ref"),
                    payload=payload,
                    rationale=str(item.get("rationale") or ""),
                    confidence=item.get("confidence"),
                    source_refs=source_refs,
                )
            except Exception as exc:
                raise GraphDraftingError("GPT graph patch operation was invalid.") from exc
            try:
                self.validate_operation(operation, operation.payload)
            except ValidationError as exc:
                raise GraphDraftingError(str(exc)) from exc
            operations.append(operation)
        return operations

    def ensure_operation_references_exist(
        self,
        operation: GraphChangeOperation,
        payload: dict[str, Any],
    ) -> None:
        if operation.op == GraphChangeOp.UPDATE:
            if operation.target_entity_id is None:
                raise ValidationError("Update operations require target_entity_id.")
            try:
                self._get_graph_entity(operation.entity_type, operation.target_entity_id)
            except NotFoundError as exc:
                raise ValidationError(
                    "Operation references an unknown "
                    f"{operation.entity_type.value} ID: {operation.target_entity_id}."
                ) from exc
        try:
            refs = _iter_payload_entity_refs(payload)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Operation payload references an invalid entity ID.") from exc
        for entity_type, entity_id in refs:
            try:
                self._get_graph_entity(entity_type, entity_id)
            except NotFoundError as exc:
                raise ValidationError(
                    f"Operation references an unknown {entity_type.value} ID: {entity_id}."
                ) from exc

    def _validate_goal_update_payload(
        self,
        operation: GraphChangeOperation,
        payload: dict[str, Any],
    ) -> None:
        if operation.op != GraphChangeOp.UPDATE or operation.entity_type != EntityType.GOAL:
            return
        if operation.target_entity_id is None:
            return
        has_attribute_update = "attributes" in payload and payload.get("attributes") is not None
        has_type_update = payload.get("goal_type") is not None
        if not has_attribute_update and not has_type_update:
            return
        goal = self._get_graph_entity(EntityType.GOAL, operation.target_entity_id)
        goal_type = payload.get("goal_type") or goal.goal_type
        attributes = payload["attributes"] if has_attribute_update else goal.attributes
        try:
            validate_goal_attributes(goal_type, attributes)
        except (PydanticValidationError, ValueError) as exc:
            raise ValidationError(f"Goal attributes are invalid: {exc}") from exc


def _payload_from_json(raw_payload: Any) -> dict[str, Any]:
    if not isinstance(raw_payload, str):
        raise GraphDraftingError("GPT graph patch payload_json must be a string.")
    try:
        parsed = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise GraphDraftingError("GPT graph patch payload_json was malformed.") from exc
    if not isinstance(parsed, dict):
        raise GraphDraftingError("GPT graph patch payload_json must decode to an object.")
    return parsed


def _normalize_source_refs(
    raw_source_refs: list[Any],
    *,
    change_set: GraphChangeSet,
) -> list[dict[str, Any]]:
    """Return canonical, source-scoped refs for every graph-draft path.

    New model responses identify their supporting notes explicitly. Older
    providers and persisted test fixtures may omit those IDs or use one of the
    historical singular keys. A single available source can be filled safely;
    a multi-source draft cannot, so the complete bundle is retained and marked
    ambiguous instead of selecting a primary note.
    """

    allowed_ids = _allowed_source_note_ids(change_set)
    if not raw_source_refs:
        return [_source_ref_fallback(allowed_ids)]

    normalized: list[dict[str, Any]] = []
    for index, raw_ref in enumerate(raw_source_refs, start=1):
        if not isinstance(raw_ref, dict):
            raise GraphDraftingError(
                f"GPT graph patch source_ref #{index} must be an object."
            )
        normalized.append(
            _normalize_source_ref(
                raw_ref,
                allowed_ids=allowed_ids,
                ref_index=index,
            )
        )
    return normalized


def _allowed_source_note_ids(change_set: GraphChangeSet) -> list[str]:
    # `source_note_id` predates the bundle-aware list and remains the canonical
    # primary source, so retain it even if a historical row has a partial list.
    raw_ids = [change_set.source_note_id, *change_set.source_note_ids]
    allowed_ids: list[str] = []
    seen: set[str] = set()
    for raw_id in raw_ids:
        note_id = str(raw_id)
        if note_id in seen:
            continue
        seen.add(note_id)
        allowed_ids.append(note_id)
    return allowed_ids


def _source_ref_fallback(allowed_ids: list[str]) -> dict[str, Any]:
    is_single_source = len(allowed_ids) == 1
    return {
        "label": "source note" if is_single_source else "batch source notes",
        "quote": "",
        "region": None,
        "source_note_ids": list(allowed_ids),
        "source_note_ids_resolution": (
            _SOURCE_NOTE_IDS_RESOLUTION_SINGLE_SOURCE_FALLBACK
            if is_single_source
            else _SOURCE_NOTE_IDS_RESOLUTION_AMBIGUOUS_BUNDLE
        ),
    }


def _normalize_source_ref(
    raw_ref: dict[str, Any],
    *,
    allowed_ids: list[str],
    ref_index: int,
) -> dict[str, Any]:
    explicit_ids = _explicit_source_note_ids(raw_ref, ref_index=ref_index)
    if explicit_ids is None:
        source_note_ids = list(allowed_ids)
        resolution = (
            _SOURCE_NOTE_IDS_RESOLUTION_SINGLE_SOURCE_FALLBACK
            if len(allowed_ids) == 1
            else _SOURCE_NOTE_IDS_RESOLUTION_AMBIGUOUS_BUNDLE
        )
    else:
        source_note_ids = explicit_ids
        resolution = _SOURCE_NOTE_IDS_RESOLUTION_EXPLICIT
        unknown_ids = [note_id for note_id in source_note_ids if note_id not in allowed_ids]
        if unknown_ids:
            raise GraphDraftingError(
                f"GPT graph patch source_ref #{ref_index} contains source_note_ids "
                f"outside this draft's source notes: {', '.join(unknown_ids)}."
            )

    normalized = {
        key: value for key, value in raw_ref.items() if key not in _SOURCE_NOTE_ID_KEYS
    }
    normalized["source_note_ids"] = source_note_ids
    normalized["source_note_ids_resolution"] = resolution
    return normalized


def _explicit_source_note_ids(
    raw_ref: dict[str, Any],
    *,
    ref_index: int,
) -> list[str] | None:
    has_plural = "source_note_ids" in raw_ref
    singular_keys = [key for key in ("source_note_id", "note_id") if key in raw_ref]
    if not has_plural and not singular_keys:
        return None

    if has_plural:
        raw_ids = raw_ref["source_note_ids"]
        if not isinstance(raw_ids, list) or not raw_ids:
            raise GraphDraftingError(
                f"GPT graph patch source_ref #{ref_index} source_note_ids must be a "
                "non-empty list."
            )
        source_note_ids = [
            _canonical_source_note_id(raw_id, ref_index=ref_index) for raw_id in raw_ids
        ]
        if len(source_note_ids) != len(set(source_note_ids)):
            raise GraphDraftingError(
                f"GPT graph patch source_ref #{ref_index} source_note_ids must be unique."
            )
        for key in singular_keys:
            singular_id = _canonical_source_note_id(raw_ref[key], ref_index=ref_index)
            if singular_id not in source_note_ids:
                raise GraphDraftingError(
                    f"GPT graph patch source_ref #{ref_index} has conflicting {key} and "
                    "source_note_ids values."
                )
        return source_note_ids

    singular_ids = [
        _canonical_source_note_id(raw_ref[key], ref_index=ref_index) for key in singular_keys
    ]
    if len(set(singular_ids)) != 1:
        raise GraphDraftingError(
            f"GPT graph patch source_ref #{ref_index} has conflicting legacy source note IDs."
        )
    return [singular_ids[0]]


def _canonical_source_note_id(raw_id: Any, *, ref_index: int) -> str:
    if isinstance(raw_id, bool) or not isinstance(raw_id, (str, UUID)):
        raise GraphDraftingError(
            f"GPT graph patch source_ref #{ref_index} contains an invalid source note ID."
        )
    try:
        return str(UUID(str(raw_id).strip()))
    except (AttributeError, TypeError, ValueError) as exc:
        raise GraphDraftingError(
            f"GPT graph patch source_ref #{ref_index} contains an invalid source note ID."
        ) from exc


def _validate_graph_patch_operation_shape(item: dict[str, Any]) -> None:
    required = {
        "client_ref",
        "op",
        "entity_type",
        "semantic_type",
        "target_entity_id",
        "payload_json",
        "rationale",
        "confidence",
        "source_refs",
    }
    missing = sorted(required - set(item))
    if missing:
        raise GraphDraftingError(
            f"GPT graph patch operation missing required fields: {', '.join(missing)}."
        )
    if not isinstance(item["rationale"], str):
        raise GraphDraftingError("GPT graph patch rationale must be a string.")
    confidence = item["confidence"]
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or confidence < 0
        or confidence > 1
    ):
        raise GraphDraftingError("GPT graph patch confidence must be between 0 and 1.")
    if not isinstance(item["source_refs"], list):
        raise GraphDraftingError("GPT graph patch source_refs must be a list.")


def _validate_graph_patch_top_level(graph_patch: dict[str, Any]) -> None:
    if not isinstance(graph_patch.get("summary"), str):
        raise GraphDraftingError("GPT graph patch summary must be a string.")
    if not isinstance(graph_patch.get("uncertain_fields"), list):
        raise GraphDraftingError("GPT graph patch uncertain_fields must be a list.")
    if not isinstance(graph_patch.get("clarification_requests"), list):
        raise GraphDraftingError("GPT graph patch clarification_requests must be a list.")


def _resolve_refs(value: Any, ref_map: dict[str, UUID]) -> Any:
    if isinstance(value, list):
        return [_resolve_refs(item, ref_map) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            ref_name = value["$ref"]
            if not isinstance(ref_name, str) or ref_name not in ref_map:
                raise ValidationError(f"Unknown graph draft ref: {ref_name}")
            return str(ref_map[ref_name])
        return {key: _resolve_refs(item, ref_map) for key, item in value.items()}
    return value


def _payload_for_review_validation(value: Any) -> Any:
    if isinstance(value, list):
        return [_payload_for_review_validation(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return _REF_VALIDATION_PLACEHOLDER
        return {key: _payload_for_review_validation(item) for key, item in value.items()}
    return value


def _validate_graph_operation_payload(
    operation: GraphChangeOperation,
    payload: dict[str, Any],
) -> None:
    candidate = _payload_for_review_validation(payload)
    if not isinstance(candidate, dict):
        raise ValidationError("Operation payload must be a JSON object.")
    if operation.op == GraphChangeOp.UPDATE and operation.target_entity_id is None:
        raise ValidationError("Update operations require target_entity_id.")
    if operation.op == GraphChangeOp.UPDATE and not candidate:
        raise ValidationError("Update operation payload must include at least one field.")
    schema_map = _CREATE_SCHEMAS if operation.op == GraphChangeOp.CREATE else _UPDATE_SCHEMAS
    schema_type = schema_map.get(operation.entity_type)
    if schema_type is None:
        raise ValidationError("Unsupported entity type.")
    _validate_payload(schema_type, candidate)


def _format_pydantic_error(exc: PydanticValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "Operation payload failed API validation."
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", []))
    message = str(first.get("msg") or "invalid value")
    if location:
        return f"Operation payload failed API validation: {location}: {message}"
    return f"Operation payload failed API validation: {message}"


def _validate_payload(schema_type: Any, payload: dict[str, Any]) -> Any:
    try:
        return schema_type.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError(_format_pydantic_error(exc)) from exc


def _validate_semantic_operation_target(operation: GraphChangeOperation) -> None:
    if operation.semantic_type is None:
        return
    if operation.semantic_type == GraphDraftSemanticType.CREATE_ENTITY:
        if operation.op != GraphChangeOp.CREATE:
            raise ValidationError("Semantic operation create_entity requires create op.")
        return
    if operation.semantic_type == GraphDraftSemanticType.UPDATE_ENTITY:
        if operation.op != GraphChangeOp.UPDATE:
            raise ValidationError("Semantic operation update_entity requires update op.")
        return
    allowed = _SEMANTIC_ALLOWED_TARGETS.get(operation.semantic_type)
    if allowed is None:
        raise ValidationError(f"Unsupported semantic operation: {operation.semantic_type.value}.")
    candidate = (operation.op, operation.entity_type)
    if candidate not in allowed:
        raise ValidationError(
            f"Semantic operation {operation.semantic_type.value} cannot be used with "
            f"{operation.op.value} {operation.entity_type.value}."
        )


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _iter_payload_entity_refs(value: Any) -> list[tuple[EntityType, UUID]]:
    refs: list[tuple[EntityType, UUID]] = []
    if isinstance(value, list):
        for item in value:
            refs.extend(_iter_payload_entity_refs(item))
        return refs
    if not isinstance(value, dict):
        return refs
    if set(value) == {"$ref"}:
        return refs
    entity_type = value.get("entity_type")
    entity_id = value.get("entity_id")
    if isinstance(entity_type, str) and isinstance(entity_id, str):
        refs.append((EntityType(entity_type), UUID(entity_id)))
    for key, item in value.items():
        if key in _ENTITY_ID_FIELDS and isinstance(item, str):
            refs.append((_ENTITY_ID_FIELDS[key], UUID(item)))
        elif key in _ENTITY_ID_LIST_FIELDS and isinstance(item, list):
            for list_item in item:
                if isinstance(list_item, str):
                    refs.append((_ENTITY_ID_LIST_FIELDS[key], UUID(list_item)))
        else:
            refs.extend(_iter_payload_entity_refs(item))
    return refs



def resolve_refs(value: Any, ref_map: dict[str, UUID]) -> Any:
    return _resolve_refs(value, ref_map)


def validate_payload(schema_type: Any, payload: dict[str, Any]) -> Any:
    return _validate_payload(schema_type, payload)


def string_list(raw: Any) -> list[str]:
    return _string_list(raw)
