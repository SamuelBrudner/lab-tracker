"""MCP server surface for LLM-driven Lab Tracker workflows."""

from __future__ import annotations

import argparse
import json
import re
from importlib import resources
from collections.abc import Callable, Iterable
from typing import Any, Protocol, TypeVar
from uuid import UUID, uuid4

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.config import Settings, get_settings
from lab_tracker.db import get_engine, get_session_factory
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    AnalysisStatus,
    ClaimInput,
    ClaimStatus,
    DatasetCommitManifestInput,
    DatasetFile,
    DatasetStatus,
    EntityRef,
    EntityType,
    NoteStatus,
    ProjectStatus,
    QuestionStatus,
    QuestionType,
    SessionStatus,
    SessionType,
    VisualizationInput,
)
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

T = TypeVar("T")
REVIEW_DASHBOARD_URI = "ui://lab-tracker/review-dashboard-v1.html"
REVIEW_DASHBOARD_MIME_TYPE = "text/html;profile=mcp-app"
CODING_GUIDE_URI = "lab-tracker://coding/guide"
CHATGPT_APP_META = {
    "ui": {"resourceUri": REVIEW_DASHBOARD_URI, "visibility": ["model", "app"]},
    "openai/outputTemplate": REVIEW_DASHBOARD_URI,
}
MCP_PROFILES = {"chatgpt", "coding"}
CODING_MCP_INSTRUCTIONS = (
    "Use this read-only MCP server when coding on Lab Tracker and live lab context, "
    "project questions, notes, sessions, or scientific workflow state would improve the "
    "code change. Inspect source files with normal coding tools; use MCP only for Lab "
    "Tracker domain context. Do not mutate lab records through this coding profile."
)
CONTEXT_SEARCH_STOP_WORDS = {
    "about",
    "after",
    "before",
    "during",
    "from",
    "into",
    "looked",
    "notes",
    "page",
    "that",
    "this",
    "with",
}


class MCPServerLike(Protocol):
    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """Register an MCP tool."""

    def resource(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """Register an MCP resource."""

    def prompt(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """Register an MCP prompt."""


class LabTrackerRuntimeLike(Protocol):
    actor: AuthContext
    enable_writes: bool
    expose_legacy_tools: bool

    def execute(self, operation: Callable[[LabTrackerAPI], T]) -> T:
        """Run one Lab Tracker operation against the configured persistence layer."""


class LabTrackerMCPRuntime:
    """Request-like unit-of-work wrapper used by MCP tool calls."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        engine: Engine | None = None,
        session_factory: sessionmaker[Session] | None = None,
        api: LabTrackerAPI | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._engine = engine or get_engine(self.settings)
        self._owns_engine = engine is None
        self._session_factory = session_factory or get_session_factory(
            settings=self.settings,
            engine=self._engine,
        )
        self._api = api or LabTrackerAPI(
            raw_storage=LocalNoteStorage(self.settings.note_storage_path),
        )
        try:
            actor_role = Role(self.settings.mcp_actor_role.strip().lower())
        except ValueError as exc:
            raise ValidationError("mcp_actor_role must be one of: admin, editor, viewer.") from exc
        self.actor = AuthContext(
            user_id=self.settings.mcp_actor_user_id,
            role=actor_role,
        )
        self.enable_writes = self.settings.mcp_enable_writes
        self.expose_legacy_tools = self.settings.mcp_expose_legacy_tools

    def execute(self, operation: Callable[[LabTrackerAPI], T]) -> T:
        with self._session_factory() as session:
            repository = SQLAlchemyLabTrackerRepository(session)
            bound_api = self._api.for_request(repository)
            committed = False
            try:
                result = operation(bound_api)
                session.commit()
                committed = True
                return result
            except Exception:
                session.rollback()
                raise
            finally:
                bound_api.finish_request(committed=committed)

    def close(self) -> None:
        if self._owns_engine:
            self._engine.dispose()


def _model_data(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _models_data(models: Iterable[Any]) -> list[dict[str, Any]]:
    return [_model_data(model) for model in models]


def _tool_result(
    structured_content: dict[str, Any],
    text: str,
    *,
    meta: dict[str, Any] | None = None,
) -> Any:
    from mcp.types import CallToolResult, TextContent

    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=structured_content,
        _meta=meta or {},
    )


def _read_annotations() -> Any:
    from mcp.types import ToolAnnotations

    return ToolAnnotations(readOnlyHint=True)


def _write_annotations() -> Any:
    from mcp.types import ToolAnnotations

    return ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        openWorldHint=False,
    )


def _read_review_dashboard_html() -> str:
    return (
        resources.files("lab_tracker.mcp_app")
        .joinpath("review-dashboard-v1.html")
        .read_text(encoding="utf-8")
    )


def _parse_uuid(value: str | UUID, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a valid UUID.") from exc


def _parse_optional_uuid(value: str | UUID | None, field_name: str) -> UUID | None:
    if value is None or value == "":
        return None
    return _parse_uuid(value, field_name)


def _parse_uuid_list(values: list[str] | None, field_name: str) -> list[UUID]:
    return [_parse_uuid(value, field_name) for value in values or []]


def _parse_enum(enum_type: type[T], value: str | T | None, field_name: str, default: T) -> T:
    if value is None or value == "":
        return default
    try:
        return enum_type(value if not isinstance(value, str) else value.strip().lower())  # type: ignore[call-arg]
    except ValueError as exc:
        valid_values = ", ".join(item.value for item in enum_type)  # type: ignore[attr-defined]
        raise ValidationError(f"{field_name} must be one of: {valid_values}.") from exc


def _parse_optional_enum(enum_type: type[T], value: str | T | None, field_name: str) -> T | None:
    if value is None or value == "":
        return None
    try:
        return enum_type(value if not isinstance(value, str) else value.strip().lower())  # type: ignore[call-arg]
    except ValueError as exc:
        valid_values = ", ".join(item.value for item in enum_type)  # type: ignore[attr-defined]
        raise ValidationError(f"{field_name} must be one of: {valid_values}.") from exc


def _string_map(value: dict[str, Any] | None, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError(f"{field_name} must be an object.")
    return {str(key): str(raw_value) for key, raw_value in value.items()}


def _parse_entity_refs(raw_targets: list[dict[str, str]] | None) -> list[EntityRef] | None:
    if raw_targets is None:
        return None
    targets: list[EntityRef] = []
    for index, raw_target in enumerate(raw_targets):
        if not isinstance(raw_target, dict):
            raise ValidationError(f"targets[{index}] must be an object.")
        if not raw_target.get("entity_type"):
            raise ValidationError(f"targets[{index}].entity_type is required.")
        entity_type = _parse_enum(
            EntityType,
            raw_target.get("entity_type"),
            f"targets[{index}].entity_type",
            EntityType.NOTE,
        )
        entity_id = _parse_uuid(raw_target.get("entity_id", ""), f"targets[{index}].entity_id")
        targets.append(EntityRef(entity_type=entity_type, entity_id=entity_id))
    return targets


def _entity_ref_key(ref: EntityRef) -> tuple[EntityType, UUID]:
    return (ref.entity_type, ref.entity_id)


def _dedupe_entity_refs(refs: Iterable[EntityRef]) -> list[EntityRef]:
    seen: set[tuple[EntityType, UUID]] = set()
    deduped: list[EntityRef] = []
    for ref in refs:
        key = _entity_ref_key(ref)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ref)
    return deduped


def _parse_proposed_questions(
    raw_questions: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for index, raw_question in enumerate(raw_questions or []):
        if not isinstance(raw_question, dict):
            raise ValidationError(f"proposed_questions[{index}] must be an object.")
        text = str(raw_question.get("text", "")).strip()
        if not text:
            raise ValidationError(f"proposed_questions[{index}].text must not be empty.")
        raw_question_type = raw_question.get("question_type", raw_question.get("type"))
        if isinstance(raw_question_type, str):
            raw_question_type = (
                raw_question_type.strip().lower().replace("-", "_").replace(" ", "_")
            )
        question_type = _parse_enum(
            QuestionType,
            raw_question_type,
            f"proposed_questions[{index}].question_type",
            QuestionType.DESCRIPTIVE,
        )
        hypothesis = raw_question.get("hypothesis")
        parent_ids = _parse_uuid_list(
            raw_question.get("parent_question_ids"),
            f"proposed_questions[{index}].parent_question_ids",
        )
        questions.append(
            {
                "text": text,
                "question_type": question_type,
                "hypothesis": str(hypothesis).strip() if hypothesis else None,
                "parent_question_ids": parent_ids,
            }
        )
    return questions


def _parse_dataset_files(raw_files: list[dict[str, Any]] | None) -> list[DatasetFile]:
    files: list[DatasetFile] = []
    for index, raw_file in enumerate(raw_files or []):
        if not isinstance(raw_file, dict):
            raise ValidationError(f"files[{index}] must be an object.")
        path = str(raw_file.get("path", "")).strip()
        checksum = str(raw_file.get("checksum", "")).strip()
        if not path:
            raise ValidationError(f"files[{index}].path must not be empty.")
        if not checksum:
            raise ValidationError(f"files[{index}].checksum must not be empty.")
        raw_size = raw_file.get("size_bytes")
        size_bytes = None if raw_size is None else int(raw_size)
        files.append(DatasetFile(path=path, checksum=checksum, size_bytes=size_bytes))
    return files


def _build_manifest(
    *,
    files: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    nwb_metadata: dict[str, Any] | None = None,
    bids_metadata: dict[str, Any] | None = None,
    note_ids: list[str] | None = None,
    source_session_id: str | None = None,
) -> DatasetCommitManifestInput | None:
    parsed_files = _parse_dataset_files(files)
    parsed_metadata = _string_map(metadata, "metadata")
    parsed_nwb_metadata = _string_map(nwb_metadata, "nwb_metadata")
    parsed_bids_metadata = _string_map(bids_metadata, "bids_metadata")
    parsed_note_ids = _parse_uuid_list(note_ids, "note_ids")
    parsed_source_session_id = _parse_optional_uuid(source_session_id, "source_session_id")
    if not any(
        [
            parsed_files,
            parsed_metadata,
            parsed_nwb_metadata,
            parsed_bids_metadata,
            parsed_note_ids,
            parsed_source_session_id,
        ]
    ):
        return None
    return DatasetCommitManifestInput(
        files=parsed_files,
        metadata=parsed_metadata,
        nwb_metadata=parsed_nwb_metadata,
        bids_metadata=parsed_bids_metadata,
        note_ids=parsed_note_ids,
        source_session_id=parsed_source_session_id,
    )


def _project_context(api: LabTrackerAPI, project_id: UUID) -> dict[str, Any]:
    project = api.get_project(project_id)
    analyses = api.list_analyses(project_id=project_id)
    claims = api.list_claims(project_id=project_id)
    datasets = api.list_datasets(project_id=project_id)
    notes = api.list_notes(project_id=project_id)
    questions = api.list_questions(project_id=project_id)
    sessions = api.list_sessions(project_id=project_id)
    visualizations = api.list_visualizations(project_id=project_id)
    return {
        "project": _model_data(project),
        "questions": _models_data(questions),
        "notes": _models_data(notes),
        "sessions": _models_data(sessions),
        "datasets": _models_data(datasets),
        "analyses": _models_data(analyses),
        "claims": _models_data(claims),
        "visualizations": _models_data(visualizations),
        "counts": {
            "questions": len(questions),
            "notes": len(notes),
            "sessions": len(sessions),
            "datasets": len(datasets),
            "analyses": len(analyses),
            "claims": len(claims),
            "visualizations": len(visualizations),
        },
    }


def _limited_project_context(
    api: LabTrackerAPI,
    project_id: UUID,
    *,
    limit: int,
) -> dict[str, Any]:
    context = _project_context(api, project_id)
    for key in [
        "questions",
        "notes",
        "sessions",
        "datasets",
        "analyses",
        "claims",
        "visualizations",
    ]:
        context[key] = context[key][:limit]
    context["limits"] = {
        "per_collection": limit,
        "counts_are_before_limits": True,
    }
    return context


def _sort_latest(items: Iterable[Any]) -> list[Any]:
    def timestamp(item: Any) -> str:
        return str(getattr(item, "updated_at", None) or getattr(item, "created_at", None) or "")

    return sorted(items, key=timestamp, reverse=True)


def _resolve_dashboard_project(
    api: LabTrackerAPI,
    project_id: UUID | None,
) -> tuple[Any | None, list[Any]]:
    projects = api.list_projects()
    if project_id is not None:
        return api.get_project(project_id), projects
    if not projects:
        return None, []
    active_projects = [project for project in projects if project.status == ProjectStatus.ACTIVE]
    return (active_projects or projects)[0], projects


def _preview_text(value: str | None, *, limit: int = 180) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}..."


def _derive_context_search_terms(
    transcribed_text: str,
    search_terms: list[str] | None,
    *,
    limit: int,
) -> list[str]:
    terms: list[str] = []
    raw_terms = search_terms or re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", transcribed_text)
    for raw_term in raw_terms:
        term = str(raw_term).strip()
        normalized = term.casefold()
        if not term or normalized in CONTEXT_SEARCH_STOP_WORDS:
            continue
        if normalized in {existing.casefold() for existing in terms}:
            continue
        terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def _draft_commit_summaries(notes: Iterable[Any], *, limit: int) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for note in _sort_latest(
        note for note in notes if note.metadata.get("draft_commit_id")
    ):
        draft_commit_id = note.metadata["draft_commit_id"]
        if draft_commit_id in seen:
            continue
        seen.add(draft_commit_id)
        target_refs = [
            {"entity_type": target.entity_type.value, "entity_id": str(target.entity_id)}
            for target in note.targets
        ]
        summaries.append(
            {
                "draft_commit_id": draft_commit_id,
                "status": note.metadata.get("draft_status", NoteStatus.STAGED.value),
                "source_label": note.metadata.get("source_label"),
                "note_id": str(note.note_id),
                "created_question_ids": [
                    str(target.entity_id)
                    for target in note.targets
                    if target.entity_type == EntityType.QUESTION
                ],
                "targets": target_refs,
                "preview": _preview_text(note.transcribed_text or note.raw_content),
                "created_at": str(note.created_at),
                "updated_at": str(note.updated_at),
            }
        )
        if len(summaries) >= limit:
            break
    return summaries


def _candidate_entry(
    *,
    entity_type: EntityType,
    entity_id: str | None,
    label: str | None,
    status: str | None,
    source: str,
) -> dict[str, str]:
    entry = {
        "entity_type": entity_type.value,
        "entity_id": entity_id or "",
        "label": _preview_text(label, limit=120),
        "source": source,
    }
    if status:
        entry["status"] = status
    return entry


def _add_candidate(
    candidates: dict[str, dict[str, str]],
    *,
    entity_type: EntityType,
    entity_id: str | None,
    label: str | None,
    status: str | None = None,
    source: str,
) -> None:
    if not entity_id:
        return
    candidates[f"{entity_type.value}:{entity_id}"] = _candidate_entry(
        entity_type=entity_type,
        entity_id=entity_id,
        label=label,
        status=status,
        source=source,
    )


def _candidate_targets(
    dashboard: dict[str, Any],
    searches: list[dict[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    question_candidates: dict[str, dict[str, str]] = {}
    note_candidates: dict[str, dict[str, str]] = {}
    session_candidates: dict[str, dict[str, str]] = {}
    for question in [
        *dashboard.get("active_questions", []),
        *dashboard.get("staged_questions", []),
    ]:
        _add_candidate(
            question_candidates,
            entity_type=EntityType.QUESTION,
            entity_id=question.get("question_id"),
            label=question.get("text"),
            status=question.get("status"),
            source="dashboard",
        )
    for session in dashboard.get("active_sessions", []):
        _add_candidate(
            session_candidates,
            entity_type=EntityType.SESSION,
            entity_id=session.get("session_id"),
            label=session.get("session_type"),
            status=session.get("status"),
            source="dashboard",
        )
    for note in dashboard.get("recent_notes", []):
        _add_candidate(
            note_candidates,
            entity_type=EntityType.NOTE,
            entity_id=note.get("note_id"),
            label=note.get("transcribed_text") or note.get("raw_content"),
            status=note.get("status"),
            source="dashboard",
        )
    for search in searches:
        for question in search["questions"]:
            _add_candidate(
                question_candidates,
                entity_type=EntityType.QUESTION,
                entity_id=question.get("question_id"),
                label=question.get("text"),
                status=question.get("status"),
                source=f"search:{search['query']}",
            )
        for note in search["notes"]:
            _add_candidate(
                note_candidates,
                entity_type=EntityType.NOTE,
                entity_id=note.get("note_id"),
                label=note.get("transcribed_text") or note.get("raw_content"),
                status=note.get("status"),
                source=f"search:{search['query']}",
            )
    return {
        "questions": list(question_candidates.values()),
        "notes": list(note_candidates.values()),
        "active_sessions": list(session_candidates.values()),
    }


def _prepare_lab_note_draft_context(
    api: LabTrackerAPI,
    project_id: UUID | None,
    *,
    transcribed_text: str,
    search_terms: list[str] | None,
    limit: int,
) -> dict[str, Any]:
    dashboard = _review_dashboard_payload(api, project_id, limit=limit)
    terms = _derive_context_search_terms(transcribed_text, search_terms, limit=limit)
    searches: list[dict[str, Any]] = []
    for term in terms:
        questions = api.search_questions(term, project_id=project_id, limit=limit)
        notes = api.search_notes(term, project_id=project_id, limit=limit)
        searches.append(
            {
                "query": term,
                "questions": _models_data(questions),
                "notes": _models_data(notes),
                "counts": {"questions": len(questions), "notes": len(notes)},
            }
        )
    candidates = _candidate_targets(dashboard, searches)
    return {
        "transcription_preview": _preview_text(transcribed_text),
        "search_terms": terms,
        "searches": searches,
        "candidate_targets": candidates,
        "dashboard": dashboard,
        "instructions": (
            "Use these existing records to choose project, target, parent question, or session "
            "IDs. Create new staged questions only when the transcription is not already "
            "covered by an existing question."
        ),
        "counts": {
            "searches": len(searches),
            "question_candidates": len(candidates["questions"]),
            "note_candidates": len(candidates["notes"]),
            "active_session_candidates": len(candidates["active_sessions"]),
        },
    }


def _review_dashboard_payload(
    api: LabTrackerAPI,
    project_id: UUID | None,
    *,
    limit: int = 5,
) -> dict[str, Any]:
    project, projects = _resolve_dashboard_project(api, project_id)
    if project is None:
        return {
            "project": None,
            "projects": [],
            "counts": {
                "projects": 0,
                "active_questions": 0,
                "staged_questions": 0,
                "recent_notes": 0,
                "active_sessions": 0,
                "draft_commits": 0,
            },
            "active_questions": [],
            "staged_questions": [],
            "recent_notes": [],
            "active_sessions": [],
            "draft_commits": [],
        }

    questions = api.list_questions(project_id=project.project_id)
    notes = api.list_notes(project_id=project.project_id)
    sessions = api.list_sessions(project_id=project.project_id)
    active_questions = [
        question for question in questions if question.status == QuestionStatus.ACTIVE
    ]
    staged_questions = [
        question for question in questions if question.status == QuestionStatus.STAGED
    ]
    active_sessions = [session for session in sessions if session.status == SessionStatus.ACTIVE]
    recent_notes = _sort_latest(notes)[:limit]
    draft_commits = _draft_commit_summaries(notes, limit=limit)
    return {
        "project": _model_data(project),
        "projects": _models_data(projects),
        "counts": {
            "projects": len(projects),
            "active_questions": len(active_questions),
            "staged_questions": len(staged_questions),
            "recent_notes": len(recent_notes),
            "active_sessions": len(active_sessions),
            "draft_commits": len(draft_commits),
        },
        "active_questions": _models_data(active_questions[:limit]),
        "staged_questions": _models_data(staged_questions[:limit]),
        "recent_notes": _models_data(recent_notes),
        "active_sessions": _models_data(active_sessions[:limit]),
        "draft_commits": draft_commits,
    }


def _coding_guide_markdown() -> str:
    return "\n".join(
        [
            "# Lab Tracker Coding Context",
            "",
            "Use this MCP profile from coding agents when a code change depends on live Lab "
            "Tracker domain context.",
            "",
            "- Query `coding_lab_context` before changing scientific workflow behavior.",
            "- Use `coding_search_lab` to find existing questions or notes relevant to the task.",
            "- Use `coding_project_context` when a project id is known and broader entity context "
            "is needed.",
            "- Inspect source files, tests, migrations, and docs with normal filesystem tools.",
            "- Keep this profile read-only; do not create, activate, close, archive, delete, or "
            "commit Lab Tracker records from coding-agent MCP access.",
        ]
    )


def _ensure_mcp_write_allowed(runtime: LabTrackerRuntimeLike) -> None:
    if not runtime.enable_writes:
        raise ValidationError(
            "MCP writes are disabled. Set LAB_TRACKER_MCP_ENABLE_WRITES=true to enable them."
        )
    if runtime.actor.role not in {Role.ADMIN, Role.EDITOR}:
        raise ValidationError("MCP write tools require an editor or admin actor role.")


def _should_register_write_tools(runtime: LabTrackerRuntimeLike) -> bool:
    return runtime.enable_writes and runtime.actor.role in {Role.ADMIN, Role.EDITOR}


def register_legacy_lab_tracker_mcp_interface(
    mcp: MCPServerLike,
    runtime: LabTrackerRuntimeLike,
) -> MCPServerLike:
    """Register the pre-ChatGPT-App granular MCP tools."""

    @mcp.tool()
    def lab_tracker_overview(project_id: str | None = None) -> dict[str, Any]:
        """Return entity counts and active working sets for the lab tracker."""

        parsed_project_id = _parse_optional_uuid(project_id, "project_id")

        def operation(api: LabTrackerAPI) -> dict[str, Any]:
            projects = (
                [api.get_project(parsed_project_id)]
                if parsed_project_id
                else api.list_projects()
            )
            questions = api.list_questions(project_id=parsed_project_id)
            notes = api.list_notes(project_id=parsed_project_id)
            sessions = api.list_sessions(project_id=parsed_project_id)
            datasets = api.list_datasets(project_id=parsed_project_id)
            analyses = api.list_analyses(project_id=parsed_project_id)
            claims = api.list_claims(project_id=parsed_project_id)
            visualizations = api.list_visualizations(project_id=parsed_project_id)
            return {
                "projects": _models_data(projects),
                "counts": {
                    "projects": len(projects),
                    "questions": len(questions),
                    "notes": len(notes),
                    "sessions": len(sessions),
                    "datasets": len(datasets),
                    "analyses": len(analyses),
                    "claims": len(claims),
                    "visualizations": len(visualizations),
                },
                "active_questions": _models_data(
                    question for question in questions if question.status == QuestionStatus.ACTIVE
                ),
                "active_sessions": _models_data(
                    session for session in sessions if session.status == SessionStatus.ACTIVE
                ),
                "staged_datasets": _models_data(
                    dataset for dataset in datasets if dataset.status == DatasetStatus.STAGED
                ),
            }

        return runtime.execute(operation)

    @mcp.tool()
    def lab_tracker_get_project_context(project_id: str) -> dict[str, Any]:
        """Return a full project context snapshot for LLM reasoning."""

        parsed_project_id = _parse_uuid(project_id, "project_id")
        return runtime.execute(lambda api: _project_context(api, parsed_project_id))

    @mcp.tool()
    def lab_tracker_list_projects(status: str | None = None) -> list[dict[str, Any]]:
        """List projects, optionally filtered by status."""

        parsed_status = _parse_optional_enum(ProjectStatus, status, "status")

        def operation(api: LabTrackerAPI) -> list[dict[str, Any]]:
            projects = api.list_projects()
            if parsed_status is not None:
                projects = [project for project in projects if project.status == parsed_status]
            return _models_data(projects)

        return runtime.execute(operation)

    @mcp.tool()
    def lab_tracker_create_project(
        name: str,
        description: str = "",
        status: str = ProjectStatus.ACTIVE.value,
    ) -> dict[str, Any]:
        """Create a project."""

        parsed_status = _parse_enum(ProjectStatus, status, "status", ProjectStatus.ACTIVE)
        return runtime.execute(
            lambda api: _model_data(
                api.create_project(
                    name=name,
                    description=description,
                    status=parsed_status,
                    actor=runtime.actor,
                )
            )
        )

    @mcp.tool()
    def lab_tracker_list_questions(
        project_id: str | None = None,
        status: str | None = None,
        question_type: str | None = None,
        search: str | None = None,
        parent_question_id: str | None = None,
        ancestor_question_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List questions with project, status, type, lineage, and text filters."""

        parsed_project_id = _parse_optional_uuid(project_id, "project_id")
        parsed_status = _parse_optional_enum(QuestionStatus, status, "status")
        parsed_question_type = _parse_optional_enum(QuestionType, question_type, "question_type")
        parsed_parent_id = _parse_optional_uuid(parent_question_id, "parent_question_id")
        parsed_ancestor_id = _parse_optional_uuid(ancestor_question_id, "ancestor_question_id")
        return runtime.execute(
            lambda api: _models_data(
                api.list_questions(
                    project_id=parsed_project_id,
                    status=parsed_status,
                    question_type=parsed_question_type,
                    search=search,
                    parent_question_id=parsed_parent_id,
                    ancestor_question_id=parsed_ancestor_id,
                )
            )
        )

    @mcp.tool()
    def lab_tracker_create_question(
        project_id: str,
        text: str,
        question_type: str,
        hypothesis: str | None = None,
        status: str = QuestionStatus.STAGED.value,
        parent_question_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a staged or active scientific question."""

        parsed_project_id = _parse_uuid(project_id, "project_id")
        parsed_question_type = _parse_enum(
            QuestionType,
            question_type,
            "question_type",
            QuestionType.OTHER,
        )
        parsed_status = _parse_enum(QuestionStatus, status, "status", QuestionStatus.STAGED)
        parsed_parent_ids = _parse_uuid_list(parent_question_ids, "parent_question_ids")
        return runtime.execute(
            lambda api: _model_data(
                api.create_question(
                    project_id=parsed_project_id,
                    text=text,
                    question_type=parsed_question_type,
                    hypothesis=hypothesis,
                    status=parsed_status,
                    parent_question_ids=parsed_parent_ids,
                    actor=runtime.actor,
                )
            )
        )

    @mcp.tool()
    def lab_tracker_update_question(
        question_id: str,
        text: str | None = None,
        question_type: str | None = None,
        hypothesis: str | None = None,
        status: str | None = None,
        parent_question_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update question text, type, hypothesis, status, or parent links."""

        parsed_question_id = _parse_uuid(question_id, "question_id")
        parsed_question_type = _parse_optional_enum(QuestionType, question_type, "question_type")
        parsed_status = _parse_optional_enum(QuestionStatus, status, "status")
        parsed_parent_ids = (
            _parse_uuid_list(parent_question_ids, "parent_question_ids")
            if parent_question_ids is not None
            else None
        )
        return runtime.execute(
            lambda api: _model_data(
                api.update_question(
                    parsed_question_id,
                    text=text,
                    question_type=parsed_question_type,
                    hypothesis=hypothesis,
                    status=parsed_status,
                    parent_question_ids=parsed_parent_ids,
                    actor=runtime.actor,
                )
            )
        )

    @mcp.tool()
    def lab_tracker_record_note(
        project_id: str,
        raw_content: str,
        transcribed_text: str | None = None,
        targets: list[dict[str, str]] | None = None,
        metadata: dict[str, Any] | None = None,
        status: str = NoteStatus.STAGED.value,
    ) -> dict[str, Any]:
        """Record a manual note and optionally attach it to project entities."""

        parsed_project_id = _parse_uuid(project_id, "project_id")
        parsed_targets = _parse_entity_refs(targets)
        parsed_metadata = _string_map(metadata, "metadata")
        parsed_status = _parse_enum(NoteStatus, status, "status", NoteStatus.STAGED)
        return runtime.execute(
            lambda api: _model_data(
                api.create_note(
                    project_id=parsed_project_id,
                    raw_content=raw_content,
                    transcribed_text=transcribed_text,
                    targets=parsed_targets,
                    metadata=parsed_metadata,
                    status=parsed_status,
                    actor=runtime.actor,
                )
            )
        )

    @mcp.tool()
    def lab_tracker_list_notes(
        project_id: str | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List notes by project, status, or target entity."""

        parsed_project_id = _parse_optional_uuid(project_id, "project_id")
        parsed_status = _parse_optional_enum(NoteStatus, status, "status")
        parsed_target_type = _parse_optional_enum(
            EntityType,
            target_entity_type,
            "target_entity_type",
        )
        parsed_target_id = _parse_optional_uuid(target_entity_id, "target_entity_id")
        return runtime.execute(
            lambda api: _models_data(
                api.list_notes(
                    project_id=parsed_project_id,
                    status=parsed_status,
                    target_entity_type=parsed_target_type,
                    target_entity_id=parsed_target_id,
                )
            )
        )

    @mcp.tool()
    def lab_tracker_search(
        query: str,
        project_id: str | None = None,
        include: list[str] | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search questions and notes by substring."""

        parsed_project_id = _parse_optional_uuid(project_id, "project_id")
        include_set = {item.strip().casefold() for item in include or ["questions", "notes"]}
        include_questions = not include_set or "questions" in include_set
        include_notes = not include_set or "notes" in include_set
        resolved_limit = max(1, min(limit, 200))

        def operation(api: LabTrackerAPI) -> dict[str, Any]:
            questions = (
                api.search_questions(query, project_id=parsed_project_id, limit=resolved_limit)
                if include_questions
                else []
            )
            notes = (
                api.search_notes(query, project_id=parsed_project_id, limit=resolved_limit)
                if include_notes
                else []
            )
            return {
                "questions": _models_data(questions),
                "notes": _models_data(notes),
                "counts": {"questions": len(questions), "notes": len(notes)},
            }

        return runtime.execute(operation)

    @mcp.tool()
    def lab_tracker_start_session(
        project_id: str,
        session_type: str = SessionType.OPERATIONAL.value,
        primary_question_id: str | None = None,
    ) -> dict[str, Any]:
        """Start an operational or scientific acquisition session."""

        parsed_project_id = _parse_uuid(project_id, "project_id")
        parsed_session_type = _parse_enum(
            SessionType,
            session_type,
            "session_type",
            SessionType.OPERATIONAL,
        )
        parsed_primary_question_id = _parse_optional_uuid(
            primary_question_id,
            "primary_question_id",
        )
        return runtime.execute(
            lambda api: _model_data(
                api.create_session(
                    project_id=parsed_project_id,
                    session_type=parsed_session_type,
                    primary_question_id=parsed_primary_question_id,
                    actor=runtime.actor,
                )
            )
        )

    @mcp.tool()
    def lab_tracker_close_session(session_id: str) -> dict[str, Any]:
        """Close an active session."""

        parsed_session_id = _parse_uuid(session_id, "session_id")
        return runtime.execute(
            lambda api: _model_data(
                api.update_session(
                    parsed_session_id,
                    status=SessionStatus.CLOSED,
                    actor=runtime.actor,
                )
            )
        )

    @mcp.tool()
    def lab_tracker_list_sessions(project_id: str | None = None) -> list[dict[str, Any]]:
        """List sessions, optionally filtered by project."""

        parsed_project_id = _parse_optional_uuid(project_id, "project_id")
        return runtime.execute(
            lambda api: _models_data(api.list_sessions(project_id=parsed_project_id))
        )

    @mcp.tool()
    def lab_tracker_register_acquisition_output(
        session_id: str,
        file_path: str,
        checksum: str,
        size_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Register a file emitted by an acquisition session."""

        parsed_session_id = _parse_uuid(session_id, "session_id")
        return runtime.execute(
            lambda api: _model_data(
                api.register_acquisition_output(
                    session_id=parsed_session_id,
                    file_path=file_path,
                    checksum=checksum,
                    size_bytes=size_bytes,
                    actor=runtime.actor,
                )
            )
        )

    @mcp.tool()
    def lab_tracker_create_dataset(
        project_id: str,
        primary_question_id: str,
        secondary_question_ids: list[str] | None = None,
        files: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        nwb_metadata: dict[str, Any] | None = None,
        bids_metadata: dict[str, Any] | None = None,
        note_ids: list[str] | None = None,
        source_session_id: str | None = None,
        status: str = DatasetStatus.STAGED.value,
    ) -> dict[str, Any]:
        """Create a dataset record with an optional commit manifest."""

        parsed_project_id = _parse_uuid(project_id, "project_id")
        parsed_primary_question_id = _parse_uuid(primary_question_id, "primary_question_id")
        parsed_secondary_ids = _parse_uuid_list(secondary_question_ids, "secondary_question_ids")
        parsed_status = _parse_enum(DatasetStatus, status, "status", DatasetStatus.STAGED)
        manifest = _build_manifest(
            files=files,
            metadata=metadata,
            nwb_metadata=nwb_metadata,
            bids_metadata=bids_metadata,
            note_ids=note_ids,
            source_session_id=source_session_id,
        )
        return runtime.execute(
            lambda api: _model_data(
                api.create_dataset(
                    project_id=parsed_project_id,
                    primary_question_id=parsed_primary_question_id,
                    secondary_question_ids=parsed_secondary_ids,
                    status=parsed_status,
                    commit_manifest=manifest,
                    actor=runtime.actor,
                )
            )
        )

    @mcp.tool()
    def lab_tracker_commit_dataset(
        dataset_id: str,
        files: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        nwb_metadata: dict[str, Any] | None = None,
        bids_metadata: dict[str, Any] | None = None,
        note_ids: list[str] | None = None,
        source_session_id: str | None = None,
        commit_hash: str | None = None,
    ) -> dict[str, Any]:
        """Commit a staged dataset, optionally replacing the commit manifest."""

        parsed_dataset_id = _parse_uuid(dataset_id, "dataset_id")
        manifest = _build_manifest(
            files=files,
            metadata=metadata,
            nwb_metadata=nwb_metadata,
            bids_metadata=bids_metadata,
            note_ids=note_ids,
            source_session_id=source_session_id,
        )
        return runtime.execute(
            lambda api: _model_data(
                api.update_dataset(
                    parsed_dataset_id,
                    status=DatasetStatus.COMMITTED,
                    commit_manifest=manifest,
                    commit_hash=commit_hash,
                    actor=runtime.actor,
                )
            )
        )

    @mcp.tool()
    def lab_tracker_list_datasets(project_id: str | None = None) -> list[dict[str, Any]]:
        """List datasets, optionally filtered by project."""

        parsed_project_id = _parse_optional_uuid(project_id, "project_id")
        return runtime.execute(
            lambda api: _models_data(api.list_datasets(project_id=parsed_project_id))
        )

    @mcp.tool()
    def lab_tracker_create_analysis(
        project_id: str,
        dataset_ids: list[str],
        method_hash: str,
        code_version: str,
        environment_hash: str | None = None,
        status: str = AnalysisStatus.STAGED.value,
    ) -> dict[str, Any]:
        """Create an analysis run linked to one or more datasets."""

        parsed_project_id = _parse_uuid(project_id, "project_id")
        parsed_dataset_ids = _parse_uuid_list(dataset_ids, "dataset_ids")
        parsed_status = _parse_enum(AnalysisStatus, status, "status", AnalysisStatus.STAGED)
        return runtime.execute(
            lambda api: _model_data(
                api.create_analysis(
                    project_id=parsed_project_id,
                    dataset_ids=parsed_dataset_ids,
                    method_hash=method_hash,
                    code_version=code_version,
                    environment_hash=environment_hash,
                    status=parsed_status,
                    actor=runtime.actor,
                )
            )
        )

    @mcp.tool()
    def lab_tracker_commit_analysis(
        analysis_id: str,
        environment_hash: str | None = None,
        claims: list[dict[str, Any]] | None = None,
        visualizations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Commit an analysis and optionally create claims and visualizations."""

        parsed_analysis_id = _parse_uuid(analysis_id, "analysis_id")
        claim_inputs = [
            ClaimInput(
                statement=str(raw_claim.get("statement", "")),
                confidence=float(raw_claim.get("confidence", 0.0)),
                status=_parse_enum(
                    ClaimStatus,
                    raw_claim.get("status"),
                    "claims.status",
                    ClaimStatus.PROPOSED,
                ),
                supported_by_dataset_ids=_parse_uuid_list(
                    raw_claim.get("supported_by_dataset_ids"),
                    "claims.supported_by_dataset_ids",
                ),
                supported_by_analysis_ids=_parse_uuid_list(
                    raw_claim.get("supported_by_analysis_ids"),
                    "claims.supported_by_analysis_ids",
                ),
            )
            for raw_claim in claims or []
        ]
        visualization_inputs = [
            VisualizationInput(
                viz_type=str(raw_visualization.get("viz_type", "")),
                file_path=str(raw_visualization.get("file_path", "")),
                caption=raw_visualization.get("caption"),
                related_claim_ids=_parse_uuid_list(
                    raw_visualization.get("related_claim_ids"),
                    "visualizations.related_claim_ids",
                ),
            )
            for raw_visualization in visualizations or []
        ]

        def operation(api: LabTrackerAPI) -> dict[str, Any]:
            analysis, created_claims, created_visualizations = api.commit_analysis(
                parsed_analysis_id,
                environment_hash=environment_hash,
                claims=claim_inputs,
                visualizations=visualization_inputs,
                actor=runtime.actor,
            )
            return {
                "analysis": _model_data(analysis),
                "claims": _models_data(created_claims),
                "visualizations": _models_data(created_visualizations),
            }

        return runtime.execute(operation)

    @mcp.tool()
    def lab_tracker_list_analyses(
        project_id: str | None = None,
        dataset_id: str | None = None,
        question_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List analyses by project, dataset, or question link."""

        parsed_project_id = _parse_optional_uuid(project_id, "project_id")
        parsed_dataset_id = _parse_optional_uuid(dataset_id, "dataset_id")
        parsed_question_id = _parse_optional_uuid(question_id, "question_id")
        return runtime.execute(
            lambda api: _models_data(
                api.list_analyses(
                    project_id=parsed_project_id,
                    dataset_id=parsed_dataset_id,
                    question_id=parsed_question_id,
                )
            )
        )

    @mcp.tool()
    def lab_tracker_create_claim(
        project_id: str,
        statement: str,
        confidence: float,
        status: str = ClaimStatus.PROPOSED.value,
        supported_by_dataset_ids: list[str] | None = None,
        supported_by_analysis_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a claim linked to supporting datasets or analyses."""

        parsed_project_id = _parse_uuid(project_id, "project_id")
        parsed_status = _parse_enum(ClaimStatus, status, "status", ClaimStatus.PROPOSED)
        parsed_dataset_ids = _parse_uuid_list(
            supported_by_dataset_ids,
            "supported_by_dataset_ids",
        )
        parsed_analysis_ids = _parse_uuid_list(
            supported_by_analysis_ids,
            "supported_by_analysis_ids",
        )
        return runtime.execute(
            lambda api: _model_data(
                api.create_claim(
                    project_id=parsed_project_id,
                    statement=statement,
                    confidence=confidence,
                    status=parsed_status,
                    supported_by_dataset_ids=parsed_dataset_ids,
                    supported_by_analysis_ids=parsed_analysis_ids,
                    actor=runtime.actor,
                )
            )
        )

    @mcp.tool()
    def lab_tracker_list_claims(
        project_id: str | None = None,
        status: str | None = None,
        dataset_id: str | None = None,
        analysis_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List claims by project, status, dataset, or analysis."""

        parsed_project_id = _parse_optional_uuid(project_id, "project_id")
        parsed_status = _parse_optional_enum(ClaimStatus, status, "status")
        parsed_dataset_id = _parse_optional_uuid(dataset_id, "dataset_id")
        parsed_analysis_id = _parse_optional_uuid(analysis_id, "analysis_id")
        return runtime.execute(
            lambda api: _models_data(
                api.list_claims(
                    project_id=parsed_project_id,
                    status=parsed_status,
                    dataset_id=parsed_dataset_id,
                    analysis_id=parsed_analysis_id,
                )
            )
        )

    @mcp.tool()
    def lab_tracker_create_visualization(
        analysis_id: str,
        viz_type: str,
        file_path: str,
        caption: str | None = None,
        related_claim_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a visualization record linked to an analysis."""

        parsed_analysis_id = _parse_uuid(analysis_id, "analysis_id")
        parsed_claim_ids = _parse_uuid_list(related_claim_ids, "related_claim_ids")
        return runtime.execute(
            lambda api: _model_data(
                api.create_visualization(
                    analysis_id=parsed_analysis_id,
                    viz_type=viz_type,
                    file_path=file_path,
                    caption=caption,
                    related_claim_ids=parsed_claim_ids,
                    actor=runtime.actor,
                )
            )
        )

    @mcp.tool()
    def lab_tracker_list_visualizations(
        project_id: str | None = None,
        analysis_id: str | None = None,
        claim_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List visualizations by project, analysis, or claim."""

        parsed_project_id = _parse_optional_uuid(project_id, "project_id")
        parsed_analysis_id = _parse_optional_uuid(analysis_id, "analysis_id")
        parsed_claim_id = _parse_optional_uuid(claim_id, "claim_id")
        return runtime.execute(
            lambda api: _models_data(
                api.list_visualizations(
                    project_id=parsed_project_id,
                    analysis_id=parsed_analysis_id,
                    claim_id=parsed_claim_id,
                )
            )
        )

    @mcp.resource("lab-tracker://project/{project_id}")
    def lab_tracker_project_resource(project_id: str) -> str:
        """Return a JSON project context resource."""

        return json.dumps(lab_tracker_get_project_context(project_id), indent=2, sort_keys=True)

    @mcp.prompt(name="lab_tracker_legacy_workflow_prompt")
    def lab_tracker_workflow_prompt(goal: str, project_id: str | None = None) -> str:
        """Prompt template for using Lab Tracker as an LLM-facing lab notebook."""

        project_hint = (
            f" Work inside project_id {project_id} unless the user explicitly redirects you."
            if project_id
            else " Start by listing projects or creating one if the user names a new project."
        )
        return (
            "You are operating Lab Tracker through MCP. Preserve scientific reasoning and "
            "provenance: questions explain why work is being done, notes capture the human "
            "record, sessions capture acquisition activity, datasets capture immutable data "
            "manifests, analyses capture computational work, and claims capture interpreted "
            "conclusions."
            f"{project_hint} For goal: {goal}\n\n"
            "First inspect existing context with lab_tracker_overview, "
            "lab_tracker_get_project_context, or lab_tracker_search. Create staged records when "
            "intent is clear but evidence is incomplete. Do not invent checksums, file paths, "
            "dataset files, claims, or confidence values; ask for missing provenance when needed."
        )

    return mcp


def register_lab_tracker_mcp_interface(
    mcp: MCPServerLike,
    runtime: LabTrackerRuntimeLike,
) -> MCPServerLike:
    """Register the ChatGPT-App-first MCP surface."""

    @mcp.resource(
        REVIEW_DASHBOARD_URI,
        name="lab_tracker_review_dashboard",
        title="Lab Tracker review dashboard",
        description="Compact ChatGPT App dashboard for captured lab context.",
        mime_type=REVIEW_DASHBOARD_MIME_TYPE,
        meta={"ui": {"resourceUri": REVIEW_DASHBOARD_URI}},
    )
    def review_dashboard_resource() -> str:
        """Return the packaged ChatGPT App review dashboard."""

        return _read_review_dashboard_html()

    @mcp.tool(
        name="lab_context",
        title="Show lab context",
        description=(
            "Return the current Lab Tracker project context, including active questions, "
            "staged questions, draft commits, recent notes, and active sessions."
        ),
        annotations=_read_annotations(),
        meta=CHATGPT_APP_META,
    )
    def lab_context(project_id: str | None = None, limit: int = 5) -> Any:
        """Return a compact project context snapshot for ChatGPT and the review widget."""

        parsed_project_id = _parse_optional_uuid(project_id, "project_id")
        resolved_limit = max(1, min(limit, 20))
        dashboard = runtime.execute(
            lambda api: _review_dashboard_payload(
                api,
                parsed_project_id,
                limit=resolved_limit,
            )
        )
        project_name = dashboard["project"]["name"] if dashboard["project"] else "No project"
        return _tool_result(
            {"dashboard": dashboard},
            f"Loaded Lab Tracker context for {project_name}.",
            meta={"dashboard": dashboard},
        )

    @mcp.tool(
        name="prepare_lab_note_draft",
        title="Prepare lab-note draft",
        description=(
            "Before structuring an uploaded lab-note transcription, query existing Lab Tracker "
            "questions, notes, active sessions, and dashboard context for reusable IDs."
        ),
        annotations=_read_annotations(),
        meta=CHATGPT_APP_META,
    )
    def prepare_lab_note_draft(
        transcribed_text: str,
        project_id: str | None = None,
        search_terms: list[str] | None = None,
        limit: int = 5,
    ) -> Any:
        """Return existing database context relevant to a lab-note transcription."""

        transcription = transcribed_text.strip()
        if not transcription:
            raise ValidationError("transcribed_text must not be empty.")
        parsed_project_id = _parse_optional_uuid(project_id, "project_id")
        resolved_limit = max(1, min(limit, 20))
        draft_context = runtime.execute(
            lambda api: _prepare_lab_note_draft_context(
                api,
                parsed_project_id,
                transcribed_text=transcription,
                search_terms=search_terms,
                limit=resolved_limit,
            )
        )
        counts = draft_context["counts"]
        return _tool_result(
            {
                "draft_context": draft_context,
                "dashboard": draft_context["dashboard"],
            },
            (
                "Queried Lab Tracker context for a lab-note draft: "
                f"{counts['question_candidates']} question candidates, "
                f"{counts['note_candidates']} note candidates, and "
                f"{counts['active_session_candidates']} active sessions."
            ),
            meta={
                "dashboard": draft_context["dashboard"],
                "draft_context": draft_context,
            },
        )

    @mcp.tool(
        name="refresh_review_dashboard",
        title="Refresh review dashboard",
        description="Refresh the Lab Tracker ChatGPT review dashboard without changing records.",
        annotations=_read_annotations(),
        meta={
            **CHATGPT_APP_META,
            "ui": {"resourceUri": REVIEW_DASHBOARD_URI, "visibility": ["app"]},
        },
    )
    def refresh_review_dashboard(project_id: str | None = None, limit: int = 5) -> Any:
        """Return the latest widget hydration payload."""

        parsed_project_id = _parse_optional_uuid(project_id, "project_id")
        resolved_limit = max(1, min(limit, 20))
        dashboard = runtime.execute(
            lambda api: _review_dashboard_payload(
                api,
                parsed_project_id,
                limit=resolved_limit,
            )
        )
        return _tool_result(
            {"dashboard": dashboard},
            "Refreshed the Lab Tracker review dashboard.",
            meta={"dashboard": dashboard},
        )

    @mcp.tool(
        name="search_lab_context",
        title="Search lab context",
        description="Search Lab Tracker questions and notes by substring.",
        annotations=_read_annotations(),
        meta=CHATGPT_APP_META,
    )
    def search_lab_context(
        query: str,
        project_id: str | None = None,
        include: list[str] | None = None,
        limit: int = 10,
    ) -> Any:
        """Search questions and notes."""

        parsed_project_id = _parse_optional_uuid(project_id, "project_id")
        include_set = {item.strip().casefold() for item in include or ["questions", "notes"]}
        include_questions = not include_set or "questions" in include_set
        include_notes = not include_set or "notes" in include_set
        resolved_limit = max(1, min(limit, 50))

        def operation(api: LabTrackerAPI) -> dict[str, Any]:
            questions = (
                api.search_questions(query, project_id=parsed_project_id, limit=resolved_limit)
                if include_questions
                else []
            )
            notes = (
                api.search_notes(query, project_id=parsed_project_id, limit=resolved_limit)
                if include_notes
                else []
            )
            return {
                "query": query,
                "questions": _models_data(questions),
                "notes": _models_data(notes),
                "counts": {"questions": len(questions), "notes": len(notes)},
            }

        results = runtime.execute(operation)
        return _tool_result(
            results,
            (
                "Found "
                f"{results['counts']['questions']} questions and "
                f"{results['counts']['notes']} notes matching {query!r}."
            ),
            meta={"search_results": results},
        )

    if _should_register_write_tools(runtime):

        @mcp.tool(
            name="draft_lab_note_commit",
            title="Draft lab-note commit",
            description=(
                "After prepare_lab_note_draft has queried existing context, convert "
                "ChatGPT's OCR/transcription from an uploaded lab-note image into one staged "
                "note plus optional staged questions for review."
            ),
            annotations=_write_annotations(),
            meta=CHATGPT_APP_META,
        )
        def draft_lab_note_commit(
            project_id: str,
            transcribed_text: str,
            summary: str | None = None,
            source_label: str | None = None,
            proposed_questions: list[dict[str, Any]] | None = None,
            target_entity_type: str | None = None,
            target_entity_id: str | None = None,
            metadata: dict[str, Any] | None = None,
        ) -> Any:
            """Create a staged draft bundle from LLM-read image notes."""

            _ensure_mcp_write_allowed(runtime)
            parsed_project_id = _parse_uuid(project_id, "project_id")
            ocr_text = transcribed_text.strip()
            if not ocr_text:
                raise ValidationError("transcribed_text must not be empty.")
            parsed_questions = _parse_proposed_questions(proposed_questions)
            explicit_targets: list[EntityRef] = []
            if target_entity_type or target_entity_id:
                if not target_entity_type or not target_entity_id:
                    raise ValidationError(
                        "target_entity_type and target_entity_id must be provided together."
                    )
                explicit_targets = _parse_entity_refs(
                    [{"entity_type": target_entity_type, "entity_id": target_entity_id}]
                ) or []
            draft_commit_id = str(uuid4())
            resolved_summary = summary.strip() if summary and summary.strip() else None
            resolved_metadata = {
                **_string_map(metadata, "metadata"),
                "created_via": "chatgpt_app",
                "source_type": "image_lab_notes",
                "draft_commit_id": draft_commit_id,
                "draft_status": NoteStatus.STAGED.value,
            }
            if source_label and source_label.strip():
                resolved_metadata["source_label"] = source_label.strip()

            def operation(api: LabTrackerAPI) -> dict[str, Any]:
                questions = [
                    api.create_question(
                        project_id=parsed_project_id,
                        text=question["text"],
                        question_type=question["question_type"],
                        hypothesis=question["hypothesis"],
                        status=QuestionStatus.STAGED,
                        parent_question_ids=question["parent_question_ids"],
                        actor=runtime.actor,
                    )
                    for question in parsed_questions
                ]
                targets = _dedupe_entity_refs(
                    [
                        *explicit_targets,
                        *[
                            EntityRef(
                                entity_type=EntityType.QUESTION,
                                entity_id=question.question_id,
                            )
                            for question in questions
                        ],
                    ]
                )
                note = api.create_note(
                    project_id=parsed_project_id,
                    raw_content=ocr_text,
                    transcribed_text=resolved_summary,
                    targets=targets,
                    metadata=resolved_metadata,
                    status=NoteStatus.STAGED,
                    actor=runtime.actor,
                )
                dashboard = _review_dashboard_payload(api, parsed_project_id)
                draft_commit = {
                    "draft_commit_id": draft_commit_id,
                    "status": NoteStatus.STAGED.value,
                    "source_label": resolved_metadata.get("source_label"),
                    "note_id": str(note.note_id),
                    "question_ids": [str(question.question_id) for question in questions],
                    "counts": {"notes": 1, "staged_questions": len(questions)},
                }
                return {
                    "draft_commit": draft_commit,
                    "note": _model_data(note),
                    "questions": _models_data(questions),
                    "dashboard": dashboard,
                }

            result = runtime.execute(operation)
            return _tool_result(
                {
                    "draft_commit": result["draft_commit"],
                    "note": result["note"],
                    "questions": result["questions"],
                    "dashboard": result["dashboard"],
                },
                (
                    "Created a draft lab-note commit with 1 staged note and "
                    f"{len(result['questions'])} staged questions."
                ),
                meta={
                    "dashboard": result["dashboard"],
                    "draft_commit": result["draft_commit"],
                },
            )

        @mcp.tool(
            name="capture_note",
            title="Capture note",
            description="Create a staged manual note, optionally targeted to a project entity.",
            annotations=_write_annotations(),
            meta=CHATGPT_APP_META,
        )
        def capture_note(
            project_id: str,
            raw_content: str,
            target_entity_type: str | None = None,
            target_entity_id: str | None = None,
            transcribed_text: str | None = None,
            metadata: dict[str, Any] | None = None,
        ) -> Any:
            """Capture a staged note."""

            _ensure_mcp_write_allowed(runtime)
            parsed_project_id = _parse_uuid(project_id, "project_id")
            targets = None
            if target_entity_type or target_entity_id:
                if not target_entity_type or not target_entity_id:
                    raise ValidationError(
                        "target_entity_type and target_entity_id must be provided together."
                    )
                targets = _parse_entity_refs(
                    [{"entity_type": target_entity_type, "entity_id": target_entity_id}]
                )
            resolved_metadata = {
                "created_via": "chatgpt_app",
                **_string_map(metadata, "metadata"),
            }

            def operation(api: LabTrackerAPI) -> dict[str, Any]:
                note = api.create_note(
                    project_id=parsed_project_id,
                    raw_content=raw_content,
                    transcribed_text=transcribed_text,
                    targets=targets,
                    metadata=resolved_metadata,
                    status=NoteStatus.STAGED,
                    actor=runtime.actor,
                )
                dashboard = _review_dashboard_payload(api, parsed_project_id)
                return {"note": _model_data(note), "dashboard": dashboard}

            result = runtime.execute(operation)
            return _tool_result(
                {"note": result["note"], "dashboard": result["dashboard"]},
                "Captured a staged Lab Tracker note.",
                meta={"dashboard": result["dashboard"]},
            )

        @mcp.tool(
            name="stage_question",
            title="Stage question",
            description="Create a staged research question for review.",
            annotations=_write_annotations(),
            meta=CHATGPT_APP_META,
        )
        def stage_question(
            project_id: str,
            text: str,
            question_type: str = QuestionType.DESCRIPTIVE.value,
            hypothesis: str | None = None,
            parent_question_ids: list[str] | None = None,
        ) -> Any:
            """Create a staged question."""

            _ensure_mcp_write_allowed(runtime)
            parsed_project_id = _parse_uuid(project_id, "project_id")
            parsed_question_type = _parse_enum(
                QuestionType,
                question_type,
                "question_type",
                QuestionType.DESCRIPTIVE,
            )
            parsed_parent_ids = _parse_uuid_list(parent_question_ids, "parent_question_ids")

            def operation(api: LabTrackerAPI) -> dict[str, Any]:
                question = api.create_question(
                    project_id=parsed_project_id,
                    text=text,
                    question_type=parsed_question_type,
                    hypothesis=hypothesis,
                    status=QuestionStatus.STAGED,
                    parent_question_ids=parsed_parent_ids,
                    actor=runtime.actor,
                )
                dashboard = _review_dashboard_payload(api, parsed_project_id)
                return {"question": _model_data(question), "dashboard": dashboard}

            result = runtime.execute(operation)
            return _tool_result(
                {"question": result["question"], "dashboard": result["dashboard"]},
                "Staged a Lab Tracker question for review.",
                meta={"dashboard": result["dashboard"]},
            )

        @mcp.tool(
            name="update_staged_question",
            title="Update staged question",
            description="Edit a staged question before activation.",
            annotations=_write_annotations(),
            meta=CHATGPT_APP_META,
        )
        def update_staged_question(
            question_id: str,
            text: str | None = None,
            question_type: str | None = None,
            hypothesis: str | None = None,
            parent_question_ids: list[str] | None = None,
        ) -> Any:
            """Update a staged question."""

            _ensure_mcp_write_allowed(runtime)
            parsed_question_id = _parse_uuid(question_id, "question_id")
            parsed_question_type = _parse_optional_enum(
                QuestionType,
                question_type,
                "question_type",
            )
            parsed_parent_ids = (
                _parse_uuid_list(parent_question_ids, "parent_question_ids")
                if parent_question_ids is not None
                else None
            )

            def operation(api: LabTrackerAPI) -> dict[str, Any]:
                existing = api.get_question(parsed_question_id)
                if existing.status != QuestionStatus.STAGED:
                    raise ValidationError("Only staged questions can be edited by ChatGPT.")
                question = api.update_question(
                    parsed_question_id,
                    text=text,
                    question_type=parsed_question_type,
                    hypothesis=hypothesis,
                    parent_question_ids=parsed_parent_ids,
                    actor=runtime.actor,
                )
                dashboard = _review_dashboard_payload(api, question.project_id)
                return {"question": _model_data(question), "dashboard": dashboard}

            result = runtime.execute(operation)
            return _tool_result(
                {"question": result["question"], "dashboard": result["dashboard"]},
                "Updated the staged Lab Tracker question.",
                meta={"dashboard": result["dashboard"]},
            )

        @mcp.tool(
            name="activate_question",
            title="Activate question",
            description="Activate a staged question so it can guide acquisition work.",
            annotations=_write_annotations(),
            meta=CHATGPT_APP_META,
        )
        def activate_question(question_id: str) -> Any:
            """Activate a staged question."""

            _ensure_mcp_write_allowed(runtime)
            parsed_question_id = _parse_uuid(question_id, "question_id")

            def operation(api: LabTrackerAPI) -> dict[str, Any]:
                question = api.update_question(
                    parsed_question_id,
                    status=QuestionStatus.ACTIVE,
                    actor=runtime.actor,
                )
                dashboard = _review_dashboard_payload(api, question.project_id)
                return {"question": _model_data(question), "dashboard": dashboard}

            result = runtime.execute(operation)
            return _tool_result(
                {"question": result["question"], "dashboard": result["dashboard"]},
                "Activated the Lab Tracker question.",
                meta={"dashboard": result["dashboard"]},
            )

        @mcp.tool(
            name="start_session",
            title="Start session",
            description="Start a non-destructive operational or scientific acquisition session.",
            annotations=_write_annotations(),
            meta=CHATGPT_APP_META,
        )
        def start_session(
            project_id: str,
            session_type: str = SessionType.OPERATIONAL.value,
            primary_question_id: str | None = None,
        ) -> Any:
            """Start a session."""

            _ensure_mcp_write_allowed(runtime)
            parsed_project_id = _parse_uuid(project_id, "project_id")
            parsed_session_type = _parse_enum(
                SessionType,
                session_type,
                "session_type",
                SessionType.OPERATIONAL,
            )
            parsed_primary_question_id = _parse_optional_uuid(
                primary_question_id,
                "primary_question_id",
            )

            def operation(api: LabTrackerAPI) -> dict[str, Any]:
                session = api.create_session(
                    project_id=parsed_project_id,
                    session_type=parsed_session_type,
                    primary_question_id=parsed_primary_question_id,
                    actor=runtime.actor,
                )
                dashboard = _review_dashboard_payload(api, parsed_project_id)
                return {"session": _model_data(session), "dashboard": dashboard}

            result = runtime.execute(operation)
            return _tool_result(
                {"session": result["session"], "dashboard": result["dashboard"]},
                "Started a Lab Tracker session.",
                meta={"dashboard": result["dashboard"]},
            )

        @mcp.tool(
            name="close_session",
            title="Close session",
            description="Close an active acquisition session without deleting records.",
            annotations=_write_annotations(),
            meta=CHATGPT_APP_META,
        )
        def close_session(session_id: str) -> Any:
            """Close a session."""

            _ensure_mcp_write_allowed(runtime)
            parsed_session_id = _parse_uuid(session_id, "session_id")

            def operation(api: LabTrackerAPI) -> dict[str, Any]:
                session = api.update_session(
                    parsed_session_id,
                    status=SessionStatus.CLOSED,
                    actor=runtime.actor,
                )
                dashboard = _review_dashboard_payload(api, session.project_id)
                return {"session": _model_data(session), "dashboard": dashboard}

            result = runtime.execute(operation)
            return _tool_result(
                {"session": result["session"], "dashboard": result["dashboard"]},
                "Closed the Lab Tracker session.",
                meta={"dashboard": result["dashboard"]},
            )

    if runtime.expose_legacy_tools:
        register_legacy_lab_tracker_mcp_interface(mcp, runtime)

    @mcp.prompt()
    def lab_tracker_workflow_prompt(goal: str, project_id: str | None = None) -> str:
        """Prompt template for using Lab Tracker as a ChatGPT App."""

        project_hint = (
            f" Work inside project_id {project_id} unless the user explicitly redirects you."
            if project_id
            else " Start by calling lab_context to inspect available projects."
        )
        return (
            "You are operating Lab Tracker as a ChatGPT App. Preserve scientific reasoning: "
            "questions explain why work is being done, notes capture the human record, and "
            "sessions capture acquisition activity. Use lab_context first. After reading an "
            "uploaded lab-note image, call prepare_lab_note_draft with the transcription and "
            "search terms before choosing structured records; reuse returned IDs when "
            "appropriate, then call draft_lab_note_commit. Use staged records for uncertain "
            "user intent, and do not invent file paths, checksums, claims, or confidence "
            f"values. {project_hint} Goal: {goal}"
        )

    return mcp


def register_lab_tracker_coding_mcp_interface(
    mcp: MCPServerLike,
    runtime: LabTrackerRuntimeLike,
) -> MCPServerLike:
    """Register the read-only coding-agent MCP surface."""

    @mcp.resource(
        CODING_GUIDE_URI,
        name="lab_tracker_coding_guide",
        title="Lab Tracker coding guide",
        description="Compact guide for using Lab Tracker MCP context while coding.",
        mime_type="text/markdown",
    )
    def coding_guide_resource() -> str:
        """Return compact coding-agent guidance."""

        return _coding_guide_markdown()

    @mcp.tool(
        name="coding_lab_context",
        title="Show coding lab context",
        description=(
            "Return a compact read-only Lab Tracker context snapshot for coding agents, "
            "including projects, active/staged questions, recent notes, and active sessions."
        ),
        annotations=_read_annotations(),
    )
    def coding_lab_context(project_id: str | None = None, limit: int = 10) -> Any:
        """Return compact live lab context for coding work."""

        parsed_project_id = _parse_optional_uuid(project_id, "project_id")
        resolved_limit = max(1, min(limit, 50))
        lab_context = runtime.execute(
            lambda api: _review_dashboard_payload(
                api,
                parsed_project_id,
                limit=resolved_limit,
            )
        )
        project_name = lab_context["project"]["name"] if lab_context["project"] else "No project"
        return _tool_result(
            {"lab_context": lab_context},
            f"Loaded read-only coding context for {project_name}.",
        )

    @mcp.tool(
        name="coding_search_lab",
        title="Search coding lab context",
        description="Search Lab Tracker questions and notes by substring for coding context.",
        annotations=_read_annotations(),
    )
    def coding_search_lab(
        query: str,
        project_id: str | None = None,
        include: list[str] | None = None,
        limit: int = 20,
    ) -> Any:
        """Search read-only Lab Tracker entities relevant to coding work."""

        parsed_project_id = _parse_optional_uuid(project_id, "project_id")
        include_set = {item.strip().casefold() for item in include or ["questions", "notes"]}
        include_questions = not include_set or "questions" in include_set
        include_notes = not include_set or "notes" in include_set
        resolved_limit = max(1, min(limit, 100))

        def operation(api: LabTrackerAPI) -> dict[str, Any]:
            questions = (
                api.search_questions(query, project_id=parsed_project_id, limit=resolved_limit)
                if include_questions
                else []
            )
            notes = (
                api.search_notes(query, project_id=parsed_project_id, limit=resolved_limit)
                if include_notes
                else []
            )
            return {
                "query": query,
                "questions": _models_data(questions),
                "notes": _models_data(notes),
                "counts": {"questions": len(questions), "notes": len(notes)},
                "limits": {"per_collection": resolved_limit},
            }

        results = runtime.execute(operation)
        return _tool_result(
            {"search_results": results},
            (
                "Found "
                f"{results['counts']['questions']} questions and "
                f"{results['counts']['notes']} notes matching {query!r}."
            ),
        )

    @mcp.tool(
        name="coding_project_context",
        title="Show coding project context",
        description=(
            "Return capped read-only project context across Lab Tracker entities for coding "
            "agents that already know the project id."
        ),
        annotations=_read_annotations(),
    )
    def coding_project_context(project_id: str, limit: int = 20) -> Any:
        """Return capped project context for coding work."""

        parsed_project_id = _parse_uuid(project_id, "project_id")
        resolved_limit = max(1, min(limit, 100))
        project_context = runtime.execute(
            lambda api: _limited_project_context(
                api,
                parsed_project_id,
                limit=resolved_limit,
            )
        )
        return _tool_result(
            {"project_context": project_context},
            f"Loaded read-only project context for {project_context['project']['name']}.",
        )

    @mcp.prompt(name="lab_tracker_coding_workflow_prompt")
    def lab_tracker_coding_workflow_prompt(task: str, project_id: str | None = None) -> str:
        """Prompt template for coding agents using Lab Tracker context."""

        project_hint = (
            f" Start with coding_project_context for project_id {project_id}."
            if project_id
            else " Start with coding_lab_context, then search if the task names lab concepts."
        )
        return (
            "You are coding on Lab Tracker. First read AGENTS.md and relevant repo docs. "
            "Use normal filesystem/search tools for source code. Use the read-only coding MCP "
            "profile when live Lab Tracker project, question, note, or session context would "
            f"change the implementation decision. {project_hint} Never mutate lab records "
            f"through coding-agent MCP access. Task: {task}"
        )

    return mcp


def build_mcp_server(
    runtime: LabTrackerRuntimeLike | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    profile: str = "chatgpt",
) -> Any:
    """Build the FastMCP server instance."""

    if profile not in MCP_PROFILES:
        raise ValidationError("MCP profile must be one of: chatgpt, coding.")
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "The MCP SDK is not installed. Install Lab Tracker with the MCP extra: "
            "pip install -e '.[mcp]'"
        ) from exc

    mcp = FastMCP(
        "Lab Tracker Coding" if profile == "coding" else "Lab Tracker",
        instructions=CODING_MCP_INSTRUCTIONS if profile == "coding" else None,
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
    )
    resolved_runtime = runtime or LabTrackerMCPRuntime()
    if profile == "coding":
        return register_lab_tracker_coding_mcp_interface(mcp, resolved_runtime)
    return register_lab_tracker_mcp_interface(mcp, resolved_runtime)


def _run_cli(argv: list[str] | None = None, *, default_profile: str = "chatgpt") -> None:
    parser = argparse.ArgumentParser(description="Run the Lab Tracker MCP server.")
    parser.add_argument(
        "--profile",
        choices=sorted(MCP_PROFILES),
        default=default_profile,
        help="MCP profile to expose. chatgpt is the default app surface; coding is read-only.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport to use. stdio is the default for local LLM clients.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for streamable-http transport.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for streamable-http transport.",
    )
    args = parser.parse_args(argv)
    mcp = build_mcp_server(host=args.host, port=args.port, profile=args.profile)
    if args.transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport=args.transport)


def main(argv: list[str] | None = None) -> None:
    _run_cli(argv, default_profile="chatgpt")


def coding_main(argv: list[str] | None = None) -> None:
    _run_cli(argv, default_profile="coding")


if __name__ == "__main__":
    main()
