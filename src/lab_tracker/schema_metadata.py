"""Source-derived schema metadata for agent-facing Lab Tracker clients."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from pydantic import BaseModel

from lab_tracker.decision_context_constants import TASK_KIND_VALUES
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    AnalysisStatus,
    ClaimStatus,
    DatasetStatus,
    EntityType,
    ExplorationNodeStatus,
    ExplorationNodeType,
    GoalLinkStatus,
    GoalRelation,
    GoalStatus,
    GoalType,
    NoteStatus,
    ProjectGroupKind,
    ProjectMembershipRole,
    ProjectStatus,
    QuestionStatus,
    QuestionType,
    SessionStatus,
    SessionType,
)
from lab_tracker.schemas import (
    AnalysisCreate,
    AnalysisUpdate,
    AssistantDecisionContextRequest,
    ClaimCreate,
    ClaimUpdate,
    DatasetCreate,
    DatasetUpdate,
    ExplorationNodeCreate,
    ExplorationNodeUpdate,
    GoalCreate,
    GoalLinkCreate,
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
from lab_tracker.services.shared import (
    _ANALYSIS_STATUS_TRANSITIONS,
    _CLAIM_STATUS_TRANSITIONS,
    _DATASET_STATUS_TRANSITIONS,
    _QUESTION_STATUS_TRANSITIONS,
    _SESSION_STATUS_TRANSITIONS,
)

JsonObject = dict[str, Any]
EnumType = type[Enum]
SchemaType = type[BaseModel]


ENTITY_TYPE_ORDER = (
    "project",
    "question",
    "note",
    "session",
    "dataset",
    "analysis",
    "claim",
    "exploration_node",
    "visualization",
    "goal",
)


def build_schema_description(*, entity_type: str | None = None) -> JsonObject:
    """Build metadata that helps clients choose valid fields and enum values."""
    normalized_entity_type = _normalize_entity_type(entity_type)
    entities = _entity_descriptions()
    if normalized_entity_type is not None:
        entities = {normalized_entity_type: entities[normalized_entity_type]}
    return {
        "assistant": {
            "decision_context": {
                "request_schema": _schema_metadata(AssistantDecisionContextRequest),
                "task_kind": {
                    "allowed_values": list(TASK_KIND_VALUES),
                    "source": "lab_tracker.decision_context_constants.TASK_KIND_VALUES",
                },
            }
        },
        "entities": entities,
        "shared_enums": _shared_enums(),
        "notes": [
            "viz_type is intentionally a free string convention, not a controlled enum.",
            "A null status_lifecycle means the API validates allowed status values but "
            "does not enforce a transition map for that entity.",
        ],
    }


def _entity_descriptions() -> dict[str, JsonObject]:
    return {
        "project": _entity_metadata(
            create_schema=ProjectCreate,
            update_schema=ProjectUpdate,
            status_enum=ProjectStatus,
            field_enums={"status": ProjectStatus},
            natural_key=[],
            status_lifecycle=None,
        ),
        "question": _entity_metadata(
            create_schema=QuestionCreate,
            update_schema=QuestionUpdate,
            status_enum=QuestionStatus,
            field_enums={"question_type": QuestionType, "status": QuestionStatus},
            natural_key=[],
            status_lifecycle=_QUESTION_STATUS_TRANSITIONS,
        ),
        "note": _entity_metadata(
            create_schema=NoteCreate,
            update_schema=NoteUpdate,
            status_enum=NoteStatus,
            field_enums={"status": NoteStatus},
            natural_key=[],
            status_lifecycle=None,
        ),
        "session": _entity_metadata(
            create_schema=SessionCreate,
            update_schema=SessionUpdate,
            status_enum=SessionStatus,
            field_enums={"session_type": SessionType, "status": SessionStatus},
            natural_key=[],
            status_lifecycle=_SESSION_STATUS_TRANSITIONS,
        ),
        "dataset": _entity_metadata(
            create_schema=DatasetCreate,
            update_schema=DatasetUpdate,
            status_enum=DatasetStatus,
            field_enums={"status": DatasetStatus},
            natural_key=[],
            status_lifecycle=_DATASET_STATUS_TRANSITIONS,
        ),
        "analysis": _entity_metadata(
            create_schema=AnalysisCreate,
            update_schema=AnalysisUpdate,
            status_enum=AnalysisStatus,
            field_enums={"status": AnalysisStatus},
            natural_key=[],
            status_lifecycle=_ANALYSIS_STATUS_TRANSITIONS,
        ),
        "claim": _entity_metadata(
            create_schema=ClaimCreate,
            update_schema=ClaimUpdate,
            status_enum=ClaimStatus,
            field_enums={"status": ClaimStatus},
            natural_key=[],
            status_lifecycle=_CLAIM_STATUS_TRANSITIONS,
        ),
        "exploration_node": _entity_metadata(
            create_schema=ExplorationNodeCreate,
            update_schema=ExplorationNodeUpdate,
            status_enum=ExplorationNodeStatus,
            field_enums={
                "node_type": ExplorationNodeType,
                "status": ExplorationNodeStatus,
            },
            natural_key=[],
            status_lifecycle=None,
        ),
        "visualization": _entity_metadata(
            create_schema=VisualizationCreate,
            update_schema=VisualizationUpdate,
            status_enum=None,
            field_enums={},
            natural_key=[],
            status_lifecycle=None,
            free_string_fields={"viz_type"},
        ),
        "goal": _entity_metadata(
            create_schema=GoalCreate,
            update_schema=GoalUpdate,
            status_enum=GoalStatus,
            field_enums={
                "goal_type": GoalType,
                "status": GoalStatus,
            },
            natural_key=[],
            status_lifecycle=None,
            related_schemas={
                "link": _schema_metadata(
                    GoalLinkCreate,
                    field_enums={
                        "entity_type": EntityType,
                        "relation": GoalRelation,
                        "link_status": GoalLinkStatus,
                    },
                )
            },
        ),
    }


def _entity_metadata(
    *,
    create_schema: SchemaType,
    update_schema: SchemaType,
    status_enum: EnumType | None,
    field_enums: Mapping[str, EnumType],
    natural_key: list[str],
    status_lifecycle: Mapping[Enum, set[Enum]] | None,
    free_string_fields: set[str] | None = None,
    related_schemas: Mapping[str, JsonObject] | None = None,
) -> JsonObject:
    payload: JsonObject = {
        "create": _schema_metadata(
            create_schema,
            field_enums=field_enums,
            free_string_fields=free_string_fields,
        ),
        "update": _schema_metadata(
            update_schema,
            field_enums=field_enums,
            free_string_fields=free_string_fields,
        ),
        "natural_key": natural_key,
        "status": _enum_metadata(status_enum) if status_enum is not None else None,
        "status_lifecycle": _transition_metadata(status_lifecycle),
    }
    if related_schemas:
        payload["related_schemas"] = dict(related_schemas)
    return payload


def _schema_metadata(
    schema_type: SchemaType,
    *,
    field_enums: Mapping[str, EnumType] | None = None,
    free_string_fields: set[str] | None = None,
) -> JsonObject:
    json_schema = schema_type.model_json_schema()
    required = set(json_schema.get("required", []))
    properties = json_schema.get("properties", {})
    field_enums = field_enums or {}
    free_string_fields = free_string_fields or set()
    fields: dict[str, JsonObject] = {}
    for field_name, field_schema in properties.items():
        if not isinstance(field_schema, dict):
            continue
        field: JsonObject = {
            "required": field_name in required,
            "nullable": _schema_allows_null(field_schema),
        }
        type_name = _field_type_summary(field_schema)
        if type_name is not None:
            field["type"] = type_name
        enum_type = field_enums.get(field_name)
        if enum_type is not None:
            field["controlled_values"] = _enum_metadata(enum_type)
        elif field_name in free_string_fields:
            field["controlled_values"] = None
            field["convention"] = "free_string"
        constraints = _field_constraints(field_schema)
        if constraints:
            field["constraints"] = constraints
        fields[field_name] = field
    return {
        "name": schema_type.__name__,
        "required_fields": list(json_schema.get("required", [])),
        "fields": fields,
    }


def _shared_enums() -> JsonObject:
    return {
        "project_status": _enum_metadata(ProjectStatus),
        "project_group_kind": _enum_metadata(ProjectGroupKind),
        "project_membership_role": _enum_metadata(ProjectMembershipRole),
        "question_status": _enum_metadata(QuestionStatus),
        "question_type": _enum_metadata(QuestionType),
        "note_status": _enum_metadata(NoteStatus),
        "session_status": _enum_metadata(SessionStatus),
        "session_type": _enum_metadata(SessionType),
        "dataset_status": _enum_metadata(DatasetStatus),
        "analysis_status": _enum_metadata(AnalysisStatus),
        "claim_status": _enum_metadata(ClaimStatus),
        "exploration_node_status": _enum_metadata(ExplorationNodeStatus),
        "exploration_node_type": _enum_metadata(ExplorationNodeType),
        "goal_status": _enum_metadata(GoalStatus),
        "goal_type": _enum_metadata(GoalType),
        "goal_relation": _enum_metadata(GoalRelation),
        "goal_link_status": _enum_metadata(GoalLinkStatus),
        "entity_type": _enum_metadata(EntityType),
    }


def _enum_metadata(enum_type: EnumType) -> JsonObject:
    return {
        "name": enum_type.__name__,
        "allowed_values": [member.value for member in enum_type],
    }


def _transition_metadata(transitions: Mapping[Enum, set[Enum]] | None) -> JsonObject | None:
    if transitions is None:
        return None
    return {
        source.value: [
            candidate.value
            for candidate in type(source)
            if candidate in targets
        ]
        for source, targets in transitions.items()
    }


def _normalize_entity_type(entity_type: str | None) -> str | None:
    if entity_type is None:
        return None
    normalized = entity_type.strip().lower()
    if not normalized:
        return None
    if normalized not in ENTITY_TYPE_ORDER:
        allowed = ", ".join(ENTITY_TYPE_ORDER)
        raise ValidationError(
            f"Unknown schema entity_type '{entity_type}'. Allowed values: {allowed}."
        )
    return normalized


def _schema_allows_null(field_schema: Mapping[str, Any]) -> bool:
    if field_schema.get("type") == "null":
        return True
    any_of = field_schema.get("anyOf")
    if not isinstance(any_of, list):
        return False
    return any(
        isinstance(option, dict) and option.get("type") == "null"
        for option in any_of
    )


def _field_type_summary(field_schema: Mapping[str, Any]) -> str | None:
    type_name = field_schema.get("type")
    if isinstance(type_name, str):
        return type_name
    any_of = field_schema.get("anyOf")
    if isinstance(any_of, list):
        labels = [
            _field_type_summary(option)
            for option in any_of
            if isinstance(option, dict) and option.get("type") != "null"
        ]
        labels = [label for label in labels if label is not None]
        if labels:
            return " | ".join(labels)
    if "$ref" in field_schema:
        ref = str(field_schema["$ref"])
        return ref.rsplit("/", maxsplit=1)[-1]
    return None


def _field_constraints(field_schema: Mapping[str, Any]) -> JsonObject:
    constraints: JsonObject = {}
    for source_key, output_key in (
        ("minimum", "minimum"),
        ("maximum", "maximum"),
        ("minLength", "min_length"),
        ("maxLength", "max_length"),
        ("minItems", "min_items"),
        ("maxItems", "max_items"),
    ):
        if source_key in field_schema:
            constraints[output_key] = field_schema[source_key]
    any_of = field_schema.get("anyOf")
    if isinstance(any_of, list):
        for option in any_of:
            if isinstance(option, dict):
                constraints.update(_field_constraints(option))
    return constraints
