"""MCP server surface for LLM-driven Lab Tracker workflows."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterable
from typing import Any, Protocol, TypeVar
from uuid import UUID

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


class MCPServerLike(Protocol):
    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """Register an MCP tool."""

    def resource(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """Register an MCP resource."""

    def prompt(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """Register an MCP prompt."""


class LabTrackerRuntimeLike(Protocol):
    actor: AuthContext

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


def register_lab_tracker_mcp_interface(
    mcp: MCPServerLike,
    runtime: LabTrackerRuntimeLike,
) -> MCPServerLike:
    """Register Lab Tracker tools, resources, and prompts on a FastMCP-like server."""

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

    @mcp.prompt()
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


def build_mcp_server(
    runtime: LabTrackerRuntimeLike | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> Any:
    """Build the FastMCP server instance."""

    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "The MCP SDK is not installed. Install Lab Tracker with the MCP extra: "
            "pip install -e '.[mcp]'"
        ) from exc

    mcp = FastMCP(
        "Lab Tracker",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
    )
    return register_lab_tracker_mcp_interface(mcp, runtime or LabTrackerMCPRuntime())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Lab Tracker MCP server.")
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
    mcp = build_mcp_server(host=args.host, port=args.port)
    if args.transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
