"""Scoped read tools for agentic graph drafting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from lab_tracker.assistant_next_questions import build_next_questions_payload
from lab_tracker.auth import AuthContext, PrincipalType, Role
from lab_tracker.decision_context_query import RepositoryDecisionContextReader
from lab_tracker.decision_context_use_case import build_decision_context
from lab_tracker.errors import AuthError
from lab_tracker.graph_drafting import GraphDraftingError
from lab_tracker.models import EntityRef, EntityType
from lab_tracker.repository import LabTrackerRepository
from lab_tracker.schema_metadata import build_schema_description
from lab_tracker.services.goal_service import GoalService
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.publication_readiness_service import PublicationReadinessService
from lab_tracker.services.shared import (
    SENSITIVE_NOTE_VALUE,
    SENSITIVITY_METADATA_KEY,
    omit_safe_metadata,
)

JsonObject = dict[str, Any]

SENSITIVE_REDACTION = "[sensitive note content redacted]"

AGENTIC_READ_TOOL_ALLOWLIST = (
    "describe_schema",
    "get_decision_context",
    "list_projects",
    "list_questions",
    "list_question_refactors",
    "list_notes",
    "search",
    "list_sessions",
    "list_datasets",
    "list_analyses",
    "list_claims",
    "list_claim_edges",
    "list_visualizations",
    "list_goals",
    "get_goal",
    "publication_readiness",
    "list_node_goals",
    "next_questions",
)


@dataclass(frozen=True)
class ScopedToolResult:
    """Result of one model-requested read tool call."""

    payload: JsonObject


class ScopedGraphDraftReadToolExecutor:
    """Run model tool calls through a per-(project, user) read scope."""

    def __init__(
        self,
        *,
        repository: LabTrackerRepository,
        authorization: ProjectAuthorizationPolicy,
        project_id: UUID,
        target_user_id: UUID,
        sensitivity_policy: str = "redact",
        goals: GoalService,
        publication_readiness: PublicationReadinessService | None = None,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self.project_id = project_id
        self.target_user_id = target_user_id
        self.sensitivity_policy = sensitivity_policy
        self._goals = goals
        self._publication_readiness = publication_readiness
        self._actor = AuthContext(
            user_id=target_user_id,
            role=Role.VIEWER,
            principal_type=PrincipalType.USER,
        )
        accessible = authorization.accessible_project_ids(self._actor)
        if accessible is None or project_id not in accessible:
            raise AuthError("Graph draft target user cannot read the batch project.")
        self._reader = RepositoryDecisionContextReader(
            repository,
            accessible_project_ids={project_id},
        )

    def mcp_tool_specs(self) -> list[JsonObject]:
        return [
            _tool_spec(
                "describe_schema",
                "Describe Lab Tracker entity fields and enums.",
                {"entity_type": _string_schema()},
            ),
            _tool_spec(
                "get_decision_context",
                "Load bounded graph context for the batch project.",
                {
                    "task_kind": _string_schema(),
                    "query": _string_schema(),
                    "project_id": _string_schema(),
                    "question_id": _string_schema(),
                    "dataset_id": _string_schema(),
                    "analysis_id": _string_schema(),
                    "claim_id": _string_schema(),
                    "visualization_id": _string_schema(),
                    "limit": _integer_schema(),
                },
                required=("task_kind", "query"),
            ),
            _tool_spec(
                "list_projects",
                "List the single scoped project visible to this run.",
                {
                    "status": _string_schema(),
                    "limit": _integer_schema(),
                    "offset": _integer_schema(),
                },
            ),
            _tool_spec(
                "list_questions",
                "List or search questions in the scoped project.",
                _list_filter_properties(
                    "question_type",
                    "search",
                    "created_by",
                    "parent_question_id",
                    "ancestor_question_id",
                ),
            ),
            _tool_spec(
                "list_question_refactors",
                "List refactor history for a scoped question.",
                {
                    "question_id": _string_schema(),
                    "limit": _integer_schema(),
                    "offset": _integer_schema(),
                },
                required=("question_id",),
            ),
            _tool_spec(
                "list_notes",
                "List notes in the scoped project with sensitive bodies gated.",
                _list_filter_properties("created_by", "target_entity_type", "target_entity_id"),
            ),
            _tool_spec(
                "search",
                "Search scoped questions and notes.",
                {
                    "query": _string_schema(),
                    "project_id": _string_schema(),
                    "include": _string_schema(),
                    "limit": _integer_schema(),
                    "offset": _integer_schema(),
                },
                required=("query",),
            ),
            _tool_spec(
                "list_sessions",
                "List sessions in the scoped project.",
                _list_filter_properties("session_type", "created_by"),
            ),
            _tool_spec(
                "list_datasets",
                "List datasets in the scoped project.",
                _list_filter_properties("created_by"),
            ),
            _tool_spec(
                "list_analyses",
                "List analyses in the scoped project.",
                _list_filter_properties("dataset_id", "question_id", "created_by"),
            ),
            _tool_spec(
                "list_claims",
                "List claims in the scoped project.",
                _list_filter_properties("dataset_id", "analysis_id", "created_by"),
            ),
            _tool_spec(
                "list_claim_edges",
                "List typed claim edges in the scoped project.",
                {
                    "claim_id": _string_schema(),
                    "target_claim_id": _string_schema(),
                    "relation": _string_schema(),
                    "limit": _integer_schema(),
                    "offset": _integer_schema(),
                },
            ),
            _tool_spec(
                "list_visualizations",
                "List visualizations in the scoped project.",
                _list_filter_properties("analysis_id", "claim_id", "created_by"),
            ),
            _tool_spec(
                "list_goals",
                "List goals in the scoped project.",
                _list_filter_properties("goal_type"),
            ),
            _tool_spec(
                "get_goal",
                "Read one goal in the scoped project.",
                {"goal_id": _string_schema()},
                required=("goal_id",),
            ),
            _tool_spec(
                "publication_readiness",
                "Check structural publication readiness for the scoped project.",
                {"project_id": _string_schema()},
            ),
            _tool_spec(
                "list_node_goals",
                "List goals linked to one scoped graph node.",
                {
                    "project_id": _string_schema(),
                    "entity_type": _string_schema(),
                    "entity_id": _string_schema(),
                    "limit": _integer_schema(),
                    "offset": _integer_schema(),
                },
                required=("entity_type", "entity_id"),
            ),
            _tool_spec(
                "next_questions",
                "Rank open questions attached to active goals in the scoped project.",
                {"project_id": _string_schema(), "limit": _integer_schema()},
            ),
        ]

    def anthropic_tool_specs(self) -> list[JsonObject]:
        return self.mcp_tool_specs()

    def execute(self, tool_name: str, arguments: JsonObject | None = None) -> ScopedToolResult:
        name = str(tool_name or "").strip()
        if name not in AGENTIC_READ_TOOL_ALLOWLIST:
            raise GraphDraftingError(f"Graph draft read tool {name!r} is not allowlisted.")
        method = getattr(self, f"_execute_{name}", None)
        if not callable(method):
            raise GraphDraftingError(f"Graph draft read tool {name!r} is not implemented.")
        payload = method(dict(arguments or {}))
        return ScopedToolResult(payload=self._apply_sensitivity(payload))

    def _execute_describe_schema(self, arguments: JsonObject) -> JsonObject:
        return {
            "data": build_schema_description(
                entity_type=_optional_str(arguments, "entity_type")
            )
        }

    def _execute_get_decision_context(self, arguments: JsonObject) -> JsonObject:
        project_id = self._scoped_project_id(arguments)
        return build_decision_context(
            self._reader,
            task_kind=_required_str(arguments, "task_kind"),
            query=_required_str(arguments, "query"),
            project_id=str(project_id),
            question_id=_optional_str(arguments, "question_id"),
            dataset_id=_optional_str(arguments, "dataset_id"),
            analysis_id=_optional_str(arguments, "analysis_id"),
            claim_id=_optional_str(arguments, "claim_id"),
            visualization_id=_optional_str(arguments, "visualization_id"),
            limit=_int_arg(arguments, "limit", 20),
        )

    def _execute_list_projects(self, arguments: JsonObject) -> JsonObject:
        return self._reader.list_projects(
            status=_optional_str(arguments, "status"),
            limit=_int_arg(arguments, "limit", 50),
            offset=_int_arg(arguments, "offset", 0),
        )

    def _execute_list_questions(self, arguments: JsonObject) -> JsonObject:
        project_id = self._scoped_project_id(arguments)
        return self._reader.list_questions(
            project_id=str(project_id),
            status=_optional_str(arguments, "status"),
            question_type=_optional_str(arguments, "question_type"),
            search=_optional_str(arguments, "search"),
            created_by=_optional_str(arguments, "created_by"),
            parent_question_id=_optional_str(arguments, "parent_question_id"),
            ancestor_question_id=_optional_str(arguments, "ancestor_question_id"),
            limit=_int_arg(arguments, "limit", 50),
            offset=_int_arg(arguments, "offset", 0),
            recent_first=True,
        )

    def _execute_list_question_refactors(self, arguments: JsonObject) -> JsonObject:
        question_id = _uuid_arg(arguments, "question_id")
        question = self._repository.questions.get(question_id)
        if question is None or question.project_id != self.project_id:
            raise GraphDraftingError("question_id is outside the graph draft read scope.")
        limit = _int_arg(arguments, "limit", 50)
        offset = _int_arg(arguments, "offset", 0)
        items, total = self._repository.query_question_refactors(
            question_id=question_id,
            limit=limit,
            offset=offset,
        )
        return _list_payload(items, total, limit, offset)

    def _execute_list_notes(self, arguments: JsonObject) -> JsonObject:
        project_id = self._scoped_project_id(arguments)
        return self._reader.list_notes(
            project_id=str(project_id),
            status=_optional_str(arguments, "status"),
            created_by=_optional_str(arguments, "created_by"),
            since=_datetime_arg(arguments, "since"),
            until=_datetime_arg(arguments, "until"),
            target_entity_type=_optional_str(arguments, "target_entity_type"),
            target_entity_id=_optional_str(arguments, "target_entity_id"),
            limit=_int_arg(arguments, "limit", 50),
            offset=_int_arg(arguments, "offset", 0),
            recent_first=True,
        )

    def _execute_search(self, arguments: JsonObject) -> JsonObject:
        project_id = self._scoped_project_id(arguments)
        return self._reader.search(
            _required_str(arguments, "query"),
            project_id=str(project_id),
            include=_optional_str(arguments, "include"),
            limit=_int_arg(arguments, "limit", 20),
            offset=_int_arg(arguments, "offset", 0),
        )

    def _execute_list_sessions(self, arguments: JsonObject) -> JsonObject:
        project_id = self._scoped_project_id(arguments)
        return self._reader.list_sessions(
            project_id=str(project_id),
            status=_optional_str(arguments, "status"),
            session_type=_optional_str(arguments, "session_type"),
            created_by=_optional_str(arguments, "created_by"),
            since=_datetime_arg(arguments, "since"),
            until=_datetime_arg(arguments, "until"),
            limit=_int_arg(arguments, "limit", 50),
            offset=_int_arg(arguments, "offset", 0),
            recent_first=True,
        )

    def _execute_list_datasets(self, arguments: JsonObject) -> JsonObject:
        project_id = self._scoped_project_id(arguments)
        return self._reader.list_datasets(
            project_id=str(project_id),
            status=_optional_str(arguments, "status"),
            created_by=_optional_str(arguments, "created_by"),
            since=_datetime_arg(arguments, "since"),
            until=_datetime_arg(arguments, "until"),
            limit=_int_arg(arguments, "limit", 50),
            offset=_int_arg(arguments, "offset", 0),
            recent_first=True,
        )

    def _execute_list_analyses(self, arguments: JsonObject) -> JsonObject:
        project_id = self._scoped_project_id(arguments)
        return self._reader.list_analyses(
            project_id=str(project_id),
            dataset_id=_optional_str(arguments, "dataset_id"),
            question_id=_optional_str(arguments, "question_id"),
            status=_optional_str(arguments, "status"),
            created_by=_optional_str(arguments, "created_by"),
            since=_datetime_arg(arguments, "since"),
            until=_datetime_arg(arguments, "until"),
            limit=_int_arg(arguments, "limit", 50),
            offset=_int_arg(arguments, "offset", 0),
            recent_first=True,
        )

    def _execute_list_claims(self, arguments: JsonObject) -> JsonObject:
        project_id = self._scoped_project_id(arguments)
        return self._reader.list_claims(
            project_id=str(project_id),
            status=_optional_str(arguments, "status"),
            dataset_id=_optional_str(arguments, "dataset_id"),
            analysis_id=_optional_str(arguments, "analysis_id"),
            created_by=_optional_str(arguments, "created_by"),
            since=_datetime_arg(arguments, "since"),
            until=_datetime_arg(arguments, "until"),
            limit=_int_arg(arguments, "limit", 50),
            offset=_int_arg(arguments, "offset", 0),
            recent_first=True,
        )

    def _execute_list_claim_edges(self, arguments: JsonObject) -> JsonObject:
        claim_id = _uuid_or_none(_optional_str(arguments, "claim_id"))
        target_claim_id = _uuid_or_none(_optional_str(arguments, "target_claim_id"))
        if claim_id is not None:
            self._require_claim_in_scope(claim_id)
        if target_claim_id is not None:
            self._require_claim_in_scope(target_claim_id)
        limit = _int_arg(arguments, "limit", 50)
        offset = _int_arg(arguments, "offset", 0)
        items, total = self._repository.query_claim_edges(
            project_id=self.project_id,
            claim_id=claim_id,
            target_claim_id=target_claim_id,
            relation=_optional_str(arguments, "relation"),
            limit=limit,
            offset=offset,
        )
        return _list_payload(items, total, limit, offset)

    def _execute_list_visualizations(self, arguments: JsonObject) -> JsonObject:
        project_id = self._scoped_project_id(arguments)
        return self._reader.list_visualizations(
            project_id=str(project_id),
            analysis_id=_optional_str(arguments, "analysis_id"),
            claim_id=_optional_str(arguments, "claim_id"),
            created_by=_optional_str(arguments, "created_by"),
            since=_datetime_arg(arguments, "since"),
            until=_datetime_arg(arguments, "until"),
            limit=_int_arg(arguments, "limit", 50),
            offset=_int_arg(arguments, "offset", 0),
            recent_first=True,
        )

    def _execute_list_goals(self, arguments: JsonObject) -> JsonObject:
        project_id = self._scoped_project_id(arguments)
        limit = _int_arg(arguments, "limit", 50)
        offset = _int_arg(arguments, "offset", 0)
        items, total = self._repository.query_goals(
            project_id=project_id,
            goal_type=_optional_str(arguments, "goal_type"),
            status=_optional_str(arguments, "status"),
            limit=limit,
            offset=offset,
            recent_first=True,
        )
        return _list_payload(items, total, limit, offset)

    def _execute_get_goal(self, arguments: JsonObject) -> JsonObject:
        goal = self._goals.get_goal(_uuid_arg(arguments, "goal_id"))
        if goal.project_id != self.project_id:
            raise GraphDraftingError("goal_id is outside the graph draft read scope.")
        return {"data": _dump(goal)}

    def _execute_publication_readiness(self, arguments: JsonObject) -> JsonObject:
        project_id = self._scoped_project_id(arguments)
        if self._publication_readiness is None:
            raise GraphDraftingError("publication_readiness is not available in this worker.")
        return {"data": _dump(self._publication_readiness.check(project_id, actor=self._actor))}

    def _execute_list_node_goals(self, arguments: JsonObject) -> JsonObject:
        project_id = self._scoped_project_id(arguments)
        target = EntityRef(
            entity_type=EntityType(_required_str(arguments, "entity_type")),
            entity_id=_uuid_arg(arguments, "entity_id"),
        )
        items = self._goals.list_node_goals(project_id=project_id, target=target)
        limit = _int_arg(arguments, "limit", 50)
        offset = _int_arg(arguments, "offset", 0)
        page = items[offset : offset + limit]
        return _list_payload(page, len(items), limit, offset)

    def _execute_next_questions(self, arguments: JsonObject) -> JsonObject:
        project_id = self._scoped_project_id(arguments)
        goals, _ = self._repository.query_goals(project_id=project_id, limit=None, offset=0)
        questions, _ = self._repository.query_questions(project_id=project_id, limit=None, offset=0)
        claims, _ = self._repository.query_claims(project_id=project_id, limit=None, offset=0)
        return build_next_questions_payload(
            [_dump(item) for item in goals],
            [_dump(item) for item in questions],
            [_dump(item) for item in claims],
            limit=_int_arg(arguments, "limit", 5),
        )

    def _scoped_project_id(self, arguments: JsonObject) -> UUID:
        raw = _optional_str(arguments, "project_id")
        if raw is None:
            arguments["project_id"] = str(self.project_id)
            return self.project_id
        try:
            project_id = UUID(raw)
        except ValueError as exc:
            raise GraphDraftingError("project_id must be a valid UUID.") from exc
        if project_id != self.project_id:
            raise GraphDraftingError("project_id is outside the graph draft read scope.")
        return project_id

    def _require_claim_in_scope(self, claim_id: UUID) -> None:
        claim = self._repository.claims.get(claim_id)
        if claim is None or claim.project_id != self.project_id:
            raise GraphDraftingError("claim_id is outside the graph draft read scope.")

    def _apply_sensitivity(self, value: Any) -> Any:
        if self.sensitivity_policy == "allow":
            return value
        return _scrub_sensitive_notes(value, policy=self.sensitivity_policy)


def _scrub_sensitive_notes(value: Any, *, policy: str) -> Any:
    if isinstance(value, list):
        return [_scrub_sensitive_notes(item, policy=policy) for item in value]
    if not isinstance(value, dict):
        return value
    scrubbed = {key: _scrub_sensitive_notes(item, policy=policy) for key, item in value.items()}
    metadata = scrubbed.get("metadata")
    is_sensitive = (
        isinstance(metadata, dict)
        and metadata.get(SENSITIVITY_METADATA_KEY) == SENSITIVE_NOTE_VALUE
        and _looks_like_note_payload(scrubbed)
    )
    if not is_sensitive:
        return scrubbed
    if policy == "omit":
        return {
            "note_id": scrubbed.get("note_id") or scrubbed.get("id"),
            "project_id": scrubbed.get("project_id"),
            "metadata": omit_safe_metadata(metadata) if isinstance(metadata, dict) else metadata,
            "sensitive_content_omitted": True,
        }
    for field in (
        "raw_content",
        "transcribed_text",
        "preview",
        "raw_content_preview",
        "transcript_text",
    ):
        if field in scrubbed and scrubbed[field]:
            scrubbed[field] = SENSITIVE_REDACTION
    scrubbed["sensitive_content_redacted"] = True
    return scrubbed


def _looks_like_note_payload(value: JsonObject) -> bool:
    return any(key in value for key in ("note_id", "raw_content", "transcribed_text"))


def _tool_spec(
    name: str,
    description: str,
    properties: JsonObject,
    *,
    required: tuple[str, ...] = (),
) -> JsonObject:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
    }


def _list_filter_properties(*extra: str) -> JsonObject:
    properties: JsonObject = {
        "project_id": _string_schema(),
        "status": _string_schema(),
        "since": _string_schema(),
        "until": _string_schema(),
        "limit": _integer_schema(),
        "offset": _integer_schema(),
    }
    for key in extra:
        properties[key] = _string_schema()
    return properties


def _string_schema() -> JsonObject:
    return {"type": "string"}


def _integer_schema() -> JsonObject:
    return {"type": "integer", "minimum": 0}


def _optional_str(arguments: JsonObject, key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _required_str(arguments: JsonObject, key: str) -> str:
    value = _optional_str(arguments, key)
    if value is None:
        raise GraphDraftingError(f"{key} is required for this graph draft read tool.")
    return value


def _int_arg(arguments: JsonObject, key: str, default: int) -> int:
    value = arguments.get(key)
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise GraphDraftingError(f"{key} must be an integer.") from exc
    return max(0, parsed)


def _uuid_arg(arguments: JsonObject, key: str) -> UUID:
    raw = _required_str(arguments, key)
    try:
        return UUID(raw)
    except ValueError as exc:
        raise GraphDraftingError(f"{key} must be a valid UUID.") from exc


def _uuid_or_none(value: str | None) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError as exc:
        raise GraphDraftingError("UUID argument is invalid.") from exc


def _datetime_arg(arguments: JsonObject, key: str) -> datetime | None:
    raw = _optional_str(arguments, key)
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GraphDraftingError(f"{key} must be an ISO timestamp.") from exc


def _dump(item: Any) -> JsonObject:
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    if isinstance(item, dict):
        return dict(item)
    return {"value": item}


def _list_payload(items: list[Any], total: int, limit: int, offset: int) -> JsonObject:
    return {
        "data": [_dump(item) for item in items],
        "meta": {"limit": limit, "offset": offset, "total": total},
    }
