"""Graph draft review service."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lab_tracker.auth import AuthContext
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.graph_drafting import (
    BATCH_PROMPT_VERSION,
    PROMPT_VERSION,
    PROVIDER,
    GraphDraftingError,
)
from lab_tracker.models import (
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftBatchRun,
    GraphDraftBatchRunStatus,
    GraphDraftBatchSettings,
    GraphDraftBatchTrigger,
    GraphDraftMode,
    Note,
    NoteStatus,
    utc_now,
)
from lab_tracker.services.analysis_service import AnalysisService
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.claim_service import ClaimService
from lab_tracker.services.dataset_service import DatasetService
from lab_tracker.services.entity_version_service import EntityVersionService
from lab_tracker.services.goal_service import GoalService
from lab_tracker.services.graph_draft_applier import GraphPatchApplier
from lab_tracker.services.graph_draft_context import (
    GraphContextBuilder,
)
from lab_tracker.services.graph_draft_context import (
    entity_id as graph_entity_id,
)
from lab_tracker.services.graph_draft_validation import GraphPatchValidator, string_list
from lab_tracker.services.note_service import NoteService
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.question_service import QuestionService
from lab_tracker.services.session_service import SessionService
from lab_tracker.services.shared import actor_user_fk, actor_user_id
from lab_tracker.services.visualization_service import VisualizationService

_BATCH_NOTE_LIMIT = 100
_BATCH_RETRY_ATTEMPTS = 3
_DEFAULT_BATCH_CADENCE_MINUTES = 24 * 60
_DEFAULT_BATCH_RUN_TIME = "06:00"
_DEFAULT_BATCH_TIMEZONE = "America/New_York"


class GraphDraftService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        questions: QuestionService,
        notes: NoteService,
        sessions: SessionService,
        datasets: DatasetService,
        analyses: AnalysisService,
        claims: ClaimService,
        visualizations: VisualizationService,
        goals: GoalService,
        versions: EntityVersionService,
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.questions = questions
        self.notes = notes
        self.sessions = sessions
        self.datasets = datasets
        self.analyses = analyses
        self.claims = claims
        self.visualizations = visualizations
        self.goals = goals
        self.versions = versions
        self.authorization = authorization
        self.context_builder = GraphContextBuilder(
            projects=projects,
            questions=questions,
            notes=notes,
            sessions=sessions,
            datasets=datasets,
            analyses=analyses,
            claims=claims,
            visualizations=visualizations,
            goals=goals,
        )
        self.patch_validator = GraphPatchValidator(
            get_graph_entity=self.context_builder.get_graph_entity,
        )
        self.patch_applier = GraphPatchApplier(
            projects=projects,
            questions=questions,
            notes=notes,
            sessions=sessions,
            datasets=datasets,
            analyses=analyses,
            claims=claims,
            visualizations=visualizations,
            goals=goals,
        )

    def create_graph_draft_from_note(
        self,
        note_id: UUID,
        *,
        draft_client: Any,
        mode: GraphDraftMode = GraphDraftMode.GRAPH_CONTEXT,
        user_hint: str | None = None,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        prepared = self.context_builder.prepare_note_sources_for_graph_draft(note_id, mode=mode)
        note = prepared["source_note"]
        self.authorization.require_contributor(note.project_id, actor=actor)
        raw_asset = prepared["primary_raw_asset"]
        cleaned_hint = user_hint.strip() if user_hint else None
        if mode == GraphDraftMode.GRAPH_CONTEXT:
            context_packet = self.context_builder.build_graph_context_packet(
                note,
                source_notes=prepared["source_notes"],
                user_hint=cleaned_hint,
                actor=actor,
            )
        elif mode == GraphDraftMode.IMAGE_ONLY:
            context_packet = self.context_builder.image_only_context_packet(
                note,
                source_notes=prepared["source_notes"],
                user_hint=cleaned_hint,
            )
        else:
            raise ValidationError("Unsupported graph draft mode.")
        change_set = GraphChangeSet(
            change_set_id=uuid4(),
            project_id=note.project_id,
            source_note_id=note.note_id,
            source_note_ids=[item.note_id for item in prepared["source_notes"]],
            source_checksum=raw_asset.checksum if raw_asset is not None else None,
            source_content_type=raw_asset.content_type if raw_asset is not None else None,
            source_filename=raw_asset.filename if raw_asset is not None else None,
            provider=getattr(draft_client, "provider", PROVIDER),
            model=getattr(draft_client, "model", "unknown"),
            prompt_version=PROMPT_VERSION,
            draft_mode=mode,
            context_packet=context_packet,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
        )
        self._save_graph_change_set(change_set)
        try:
            graph_patch = self._draft_graph_patch(
                draft_client,
                graph_context=context_packet,
                user_hint=cleaned_hint,
                draft_mode=mode,
                source_artifacts=prepared["source_artifacts"],
                image_bytes=prepared["image_bytes"],
                image_content_type=prepared["image_content_type"],
            )
            self.patch_validator.validate_top_level(graph_patch)
            change_set.operations = self.patch_validator.operations_from_graph_patch(
                change_set,
                graph_patch,
            )
            change_set.summary = str(graph_patch.get("summary") or "")
            change_set.uncertain_fields = string_list(graph_patch.get("uncertain_fields"))
            change_set.clarification_requests = string_list(
                graph_patch.get("clarification_requests")
            )
            change_set.status = GraphChangeSetStatus.READY
            change_set.error_metadata = {}
        except GraphDraftingError as exc:
            change_set.status = GraphChangeSetStatus.FAILED
            change_set.error_metadata = {"message": str(exc)}
        finally:
            change_set.updated_at = utc_now()
            self._save_graph_change_set(change_set)
        return change_set

    def create_batch_graph_draft(
        self,
        notes: list[Note],
        *,
        draft_client: Any,
        user_hint: str | None = None,
        actor: AuthContext | None = None,
        window: tuple[datetime, datetime] | None = None,
        batch_key: str | None = None,
        max_attempts: int = _BATCH_RETRY_ATTEMPTS,
        retry_backoff_seconds: float = 0.0,
    ) -> GraphChangeSet:
        batch_notes = sorted(notes, key=lambda item: (item.created_at, str(item.note_id)))
        if not batch_notes:
            raise ValidationError("Batch graph drafting requires at least one note.")
        project_ids = {note.project_id for note in batch_notes}
        if len(project_ids) != 1:
            raise ValidationError("Batch graph drafts must be scoped to one project.")
        project_id = next(iter(project_ids))
        self.authorization.require_contributor(project_id, actor=actor)
        non_staged = [note.note_id for note in batch_notes if note.status != NoteStatus.STAGED]
        if non_staged:
            raise ValidationError("Batch graph drafts can only include staged notes.")
        cleaned_hint = user_hint.strip() if user_hint else None
        primary_note = batch_notes[0]
        primary_raw_asset = primary_note.raw_asset
        note_ids = [note.note_id for note in batch_notes]
        if batch_key is None:
            since, until = window if window is not None else (batch_notes[0].created_at, utc_now())
            batch_key = _batch_key(
                project_id=project_id,
                since=since,
                until=until,
                note_ids=note_ids,
            )
        existing = self.list_graph_change_sets(
            draft_mode=GraphDraftMode.GRAPH_BATCH,
            batch_key=batch_key,
        )
        if existing:
            return existing[0]
        context_packet = self.context_builder.build_batch_graph_context(
            batch_notes,
            window=window,
            actor=actor,
            batch_note_limit=_BATCH_NOTE_LIMIT,
        )
        if cleaned_hint:
            context_packet["user_hint"] = cleaned_hint
        change_set = GraphChangeSet(
            change_set_id=uuid4(),
            project_id=project_id,
            source_note_id=primary_note.note_id,
            source_note_ids=note_ids,
            source_checksum=primary_raw_asset.checksum if primary_raw_asset is not None else None,
            source_content_type=(
                primary_raw_asset.content_type if primary_raw_asset is not None else None
            ),
            source_filename=primary_raw_asset.filename if primary_raw_asset is not None else None,
            batch_key=batch_key,
            batch_window_start=window[0] if window is not None else None,
            batch_window_end=window[1] if window is not None else None,
            provider=getattr(draft_client, "provider", PROVIDER),
            model=getattr(draft_client, "model", "unknown"),
            prompt_version=BATCH_PROMPT_VERSION,
            draft_mode=GraphDraftMode.GRAPH_BATCH,
            context_packet=context_packet,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
        )
        self._save_graph_change_set(change_set)

        attempts = max(1, max_attempts)
        last_error: GraphDraftingError | None = None
        for attempt in range(1, attempts + 1):
            try:
                graph_patch = draft_client.draft_from_batch(
                    batch_context=context_packet,
                    user_hint=cleaned_hint,
                )
                break
            except GraphDraftingError as exc:
                last_error = exc
                if attempt >= attempts:
                    change_set.status = GraphChangeSetStatus.FAILED
                    change_set.error_metadata = {
                        "category": "model_error",
                        "message": str(exc),
                        "attempts": attempt,
                        "input_snapshot": _batch_input_snapshot(context_packet),
                    }
                    change_set.updated_at = utc_now()
                    self._save_graph_change_set(change_set)
                    return change_set
                if retry_backoff_seconds > 0:
                    time.sleep(retry_backoff_seconds * attempt)
        else:
            message = str(last_error) if last_error is not None else "Model did not return a patch."
            change_set.status = GraphChangeSetStatus.FAILED
            change_set.error_metadata = {
                "category": "model_error",
                "message": message,
                "attempts": attempts,
                "input_snapshot": _batch_input_snapshot(context_packet),
            }
            change_set.updated_at = utc_now()
            self._save_graph_change_set(change_set)
            return change_set

        try:
            self.patch_validator.validate_top_level(graph_patch)
            operations = self.patch_validator.operations_from_graph_patch(change_set, graph_patch)
            _attach_batch_source_traceability(operations, note_ids)
        except GraphDraftingError as exc:
            change_set.status = GraphChangeSetStatus.FAILED
            change_set.error_metadata = {
                "category": "validation_error",
                "message": str(exc),
                "attempts": attempts if last_error is not None else 1,
                "input_snapshot": _batch_input_snapshot(context_packet),
            }
            change_set.updated_at = utc_now()
            self._save_graph_change_set(change_set)
            return change_set

        change_set.operations = operations
        change_set.summary = str(graph_patch.get("summary") or "")
        change_set.uncertain_fields = string_list(graph_patch.get("uncertain_fields"))
        change_set.clarification_requests = string_list(graph_patch.get("clarification_requests"))
        change_set.status = GraphChangeSetStatus.READY
        change_set.error_metadata = {}
        change_set.updated_at = utc_now()
        self._save_graph_change_set(change_set)
        return change_set

    def get_graph_draft_batch_settings(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> GraphDraftBatchSettings:
        self.authorization.require_read(project_id, actor=actor)
        settings = self.repository.get_graph_draft_batch_settings_by_project(project_id)
        if settings is not None:
            return settings
        settings = _default_batch_settings(project_id=project_id, actor=actor)
        with self.unit_of_work() as repository:
            repository.graph_draft_batch_settings.save(settings)
        return settings

    def update_graph_draft_batch_settings(
        self,
        project_id: UUID,
        *,
        enabled: bool | None = None,
        cadence_minutes: int | None = None,
        run_at_local_time: str | None = None,
        timezone_name: str | None = None,
        actor: AuthContext | None = None,
    ) -> GraphDraftBatchSettings:
        self.authorization.require_owner(project_id, actor=actor)
        settings = self.repository.get_graph_draft_batch_settings_by_project(project_id)
        if settings is None:
            settings = _default_batch_settings(project_id=project_id, actor=actor)
        if enabled is not None:
            settings.enabled = enabled
        if cadence_minutes is not None:
            if cadence_minutes < 60:
                raise ValidationError("cadence_minutes must be at least 60.")
            settings.cadence_minutes = cadence_minutes
        if run_at_local_time is not None:
            _validate_run_at_local_time(run_at_local_time)
            settings.run_at_local_time = run_at_local_time
        if timezone_name is not None:
            _zoneinfo(timezone_name)
            settings.timezone_name = timezone_name
        settings.next_run_at = _next_run_at(
            cadence_minutes=settings.cadence_minutes,
            run_at_local_time=settings.run_at_local_time,
            timezone_name=settings.timezone_name,
        )
        settings.updated_at = utc_now()
        settings.updated_by = actor_user_id(actor)
        with self.unit_of_work() as repository:
            repository.graph_draft_batch_settings.save(settings)
        return settings

    def run_graph_draft_batch_for_project(
        self,
        project_id: UUID,
        *,
        draft_client: Any,
        since: datetime | None = None,
        until: datetime | None = None,
        trigger: GraphDraftBatchTrigger = GraphDraftBatchTrigger.MANUAL,
        user_hint: str | None = None,
        actor: AuthContext | None = None,
    ) -> GraphDraftBatchRun:
        self.authorization.require_contributor(project_id, actor=actor)
        self.projects.get_project(project_id)
        window_end = _as_utc(until or utc_now())
        latest_success = self.repository.latest_successful_graph_draft_batch_run(project_id)
        window_start = _as_utc(
            since
            or (latest_success.window_end if latest_success is not None else datetime(1970, 1, 1))
        )
        notes = _staged_notes_in_window(
            self.notes.list_notes(project_id=project_id),
            since=window_start,
            until=window_end,
        )
        note_ids = [note.note_id for note in notes]
        batch_key = _batch_key(
            project_id=project_id,
            since=window_start,
            until=window_end,
            note_ids=note_ids,
        )
        existing = self.repository.get_graph_draft_batch_run_by_key(batch_key)
        if existing is not None:
            return existing
        run = GraphDraftBatchRun(
            run_id=uuid4(),
            project_id=project_id,
            trigger=trigger,
            status=GraphDraftBatchRunStatus.RUNNING,
            window_start=window_start,
            window_end=window_end,
            note_count=len(notes),
            batch_key=batch_key,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
        )
        with self.unit_of_work() as repository:
            repository.graph_draft_batch_runs.save(run)
        if not notes:
            run.status = GraphDraftBatchRunStatus.SKIPPED
            run.summary = "No staged notes landed in this batch window."
            run.finished_at = utc_now()
            run.updated_at = run.finished_at
            with self.unit_of_work() as repository:
                repository.graph_draft_batch_runs.save(run)
            return run
        try:
            change_set = self.create_batch_graph_draft(
                notes,
                draft_client=draft_client,
                user_hint=user_hint,
                actor=actor,
                window=(window_start, window_end),
                batch_key=batch_key,
            )
        except Exception as exc:
            run.status = GraphDraftBatchRunStatus.FAILED
            run.summary = "Batch draft failed before a change set could be stored."
            run.error_metadata = {
                "category": "runner_error",
                "message": str(exc),
            }
            run.finished_at = utc_now()
            run.updated_at = run.finished_at
            with self.unit_of_work() as repository:
                repository.graph_draft_batch_runs.save(run)
            return run
        run.change_set_id = change_set.change_set_id
        run.summary = change_set.summary
        run.error_metadata = dict(change_set.error_metadata)
        run.status = (
            GraphDraftBatchRunStatus.READY
            if change_set.status == GraphChangeSetStatus.READY
            else GraphDraftBatchRunStatus.FAILED
        )
        run.finished_at = utc_now()
        run.updated_at = run.finished_at
        with self.unit_of_work() as repository:
            repository.graph_draft_batch_runs.save(run)
        return run

    def run_due_graph_draft_batches(
        self,
        *,
        draft_client_factory: Any,
        app_settings: Any,
        actor: AuthContext | None = None,
        now: datetime | None = None,
    ) -> list[GraphDraftBatchRun]:
        if not self.authorization.has_global_admin(actor):
            raise ValidationError("Only admins can run scheduled batch drafts.")
        current_time = _as_utc(now or utc_now())
        due_settings = self.repository.list_due_graph_draft_batch_settings(current_time)
        runs: list[GraphDraftBatchRun] = []
        for batch_settings in due_settings:
            draft_client = draft_client_factory(app_settings)
            try:
                run = self.run_graph_draft_batch_for_project(
                    batch_settings.project_id,
                    draft_client=draft_client,
                    until=current_time,
                    trigger=GraphDraftBatchTrigger.SCHEDULED,
                    actor=actor,
                )
            finally:
                close = getattr(draft_client, "close", None)
                if callable(close):
                    close()
            runs.append(run)
            batch_settings.next_run_at = _next_run_at(
                cadence_minutes=batch_settings.cadence_minutes,
                run_at_local_time=batch_settings.run_at_local_time,
                timezone_name=batch_settings.timezone_name,
                now=current_time,
            )
            batch_settings.updated_at = utc_now()
            batch_settings.updated_by = actor_user_id(actor)
            with self.unit_of_work() as repository:
                repository.graph_draft_batch_settings.save(batch_settings)
        return runs

    def list_graph_draft_batch_runs(
        self,
        *,
        project_id: UUID | None = None,
        status: GraphDraftBatchRunStatus | None = None,
    ) -> list[GraphDraftBatchRun]:
        return self.query_from_repository(
            loader=lambda repository: repository.query_graph_draft_batch_runs(
                project_id=project_id,
                status=status.value if status is not None else None,
                limit=None,
                offset=0,
            ),
        )

    def get_graph_change_set(self, change_set_id: UUID) -> GraphChangeSet:
        change_set = self.repository.graph_change_sets.get(change_set_id)
        if change_set is None:
            raise NotFoundError("Graph draft does not exist.")
        return change_set

    def list_graph_change_sets(
        self,
        *,
        project_id: UUID | None = None,
        status: GraphChangeSetStatus | None = None,
        source_note_id: UUID | None = None,
        draft_mode: GraphDraftMode | None = None,
        batch_key: str | None = None,
    ) -> list[GraphChangeSet]:
        change_sets, _ = self.query_graph_change_sets(
            project_id=project_id,
            status=status,
            source_note_id=source_note_id,
            draft_mode=draft_mode,
            batch_key=batch_key,
            limit=None,
            offset=0,
        )
        return change_sets

    def query_graph_change_sets(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: GraphChangeSetStatus | None = None,
        source_note_id: UUID | None = None,
        draft_mode: GraphDraftMode | None = None,
        batch_key: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        include_operations: bool = True,
    ) -> tuple[list[GraphChangeSet], int]:
        return self.repository.query_graph_change_sets(
            project_id=project_id,
            project_ids=project_ids,
            status=status.value if status is not None else None,
            source_note_id=source_note_id,
            draft_mode=draft_mode.value if draft_mode is not None else None,
            batch_key=batch_key,
            limit=limit,
            offset=offset,
            include_operations=include_operations,
        )

    def list_batch_graph_drafts(
        self,
        *,
        project_id: UUID | None = None,
        status: GraphChangeSetStatus | None = None,
    ) -> list[GraphChangeSet]:
        return self.list_graph_change_sets(
            project_id=project_id,
            status=status,
            draft_mode=GraphDraftMode.GRAPH_BATCH,
        )

    def update_graph_change_operation(
        self,
        change_set_id: UUID,
        operation_id: UUID,
        *,
        payload: dict[str, Any] | None = None,
        status: GraphChangeOperationStatus | None = None,
        review_note: str | None = None,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        change_set = self.get_graph_change_set(change_set_id)
        self._ensure_graph_change_set_editable(change_set, actor=actor)
        operation = self._find_graph_operation(change_set, operation_id)
        if payload is not None:
            if not isinstance(payload, dict):
                raise ValidationError("payload must be a JSON object.")
            if payload != operation.payload:
                operation.error_metadata = {
                    **operation.error_metadata,
                    "edited_at": utc_now().isoformat(),
                    "edited_by": actor_user_id(actor),
                }
            operation.payload = payload
        if review_note is not None:
            operation.review_note = review_note.strip() or None
        if status is not None:
            if status not in {
                GraphChangeOperationStatus.PROPOSED,
                GraphChangeOperationStatus.ACCEPTED,
                GraphChangeOperationStatus.REJECTED,
            }:
                raise ValidationError("Operation status must be proposed, accepted, or rejected.")
            operation.status = status
        if operation.status == GraphChangeOperationStatus.REJECTED:
            operation.error_metadata = {
                **operation.error_metadata,
                "reviewed_at": utc_now().isoformat(),
                "reviewed_by": actor_user_id(actor),
                "review_note": operation.review_note,
            }
        else:
            try:
                self.patch_validator.validate_operation(operation, operation.payload)
                operation.error_metadata = {
                    key: value
                    for key, value in operation.error_metadata.items()
                    if key
                    in {
                        "edited_at",
                        "edited_by",
                        "reviewed_at",
                        "reviewed_by",
                        "review_note",
                    }
                    and value is not None
                }
            except ValidationError as exc:
                operation.error_metadata = {
                    **operation.error_metadata,
                    "message": str(exc),
                }
                if operation.status == GraphChangeOperationStatus.ACCEPTED:
                    operation.status = GraphChangeOperationStatus.PROPOSED
        operation.updated_at = utc_now()
        change_set.updated_at = utc_now()
        self._save_graph_change_set(change_set)
        return change_set

    def submit_graph_change_set(
        self,
        change_set_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        change_set = self.get_graph_change_set(change_set_id)
        self.authorization.require_contributor(change_set.project_id, actor=actor)
        if not self._is_graph_change_set_author(
            change_set, actor
        ) and not self.authorization.has_global_write(actor):
            raise ValidationError("Only the graph draft author can submit this draft.")
        if change_set.status not in {
            GraphChangeSetStatus.READY,
            GraphChangeSetStatus.CHANGES_REQUESTED,
        }:
            raise ValidationError("Only ready or changes-requested graph drafts can be submitted.")
        change_set.status = GraphChangeSetStatus.SUBMITTED
        change_set.submitted_at = utc_now()
        change_set.submitted_by = actor_user_id(actor)
        change_set.reviewed_at = None
        change_set.reviewed_by = None
        change_set.review_note = None
        change_set.updated_at = change_set.submitted_at
        self._save_graph_change_set(change_set)
        return change_set

    def review_graph_change_set(
        self,
        change_set_id: UUID,
        *,
        status: GraphChangeSetStatus,
        note: str | None = None,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        change_set = self.get_graph_change_set(change_set_id)
        self.authorization.require_owner(change_set.project_id, actor=actor)
        if status not in {
            GraphChangeSetStatus.CHANGES_REQUESTED,
            GraphChangeSetStatus.REJECTED,
        }:
            raise ValidationError("Review status must be changes_requested or rejected.")
        if change_set.status != GraphChangeSetStatus.SUBMITTED:
            raise ValidationError("Only submitted graph drafts can be reviewed.")
        change_set.status = status
        change_set.reviewed_at = utc_now()
        change_set.reviewed_by = actor_user_id(actor)
        change_set.review_note = note.strip() if note else None
        change_set.updated_at = change_set.reviewed_at
        self._save_graph_change_set(change_set)
        return change_set

    def revise_graph_change_set(
        self,
        change_set_id: UUID,
        *,
        feedback: str,
        draft_client: Any,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        """Regenerate the whole proposed operation set from reviewer feedback.

        Reuses the same model + validation + persistence path as the initial
        draft, but seeds the model with the current operations and the reviewer's
        feedback. A model/validation failure leaves the existing draft intact.
        """
        change_set = self.get_graph_change_set(change_set_id)
        self._ensure_graph_change_set_editable(change_set, actor=actor)
        cleaned = (feedback or "").strip()
        if not cleaned:
            raise ValidationError("Reviewer feedback is required to revise a draft.")
        mode = change_set.draft_mode
        prepared = self.context_builder.prepare_note_sources_for_graph_draft(
            change_set.source_note_id,
            mode=mode,
        )
        note = prepared["source_note"]
        revise_hint = self._compose_revise_hint(change_set.operations, cleaned)
        if mode == GraphDraftMode.GRAPH_CONTEXT:
            context_packet = self.context_builder.build_graph_context_packet(
                note,
                source_notes=prepared["source_notes"],
                user_hint=revise_hint,
                actor=actor,
            )
        elif mode == GraphDraftMode.IMAGE_ONLY:
            context_packet = self.context_builder.image_only_context_packet(
                note,
                source_notes=prepared["source_notes"],
                user_hint=revise_hint,
            )
        else:
            raise ValidationError("Unsupported graph draft mode.")
        try:
            graph_patch = self._draft_graph_patch(
                draft_client,
                graph_context=context_packet,
                user_hint=revise_hint,
                draft_mode=mode,
                source_artifacts=prepared["source_artifacts"],
                image_bytes=prepared["image_bytes"],
                image_content_type=prepared["image_content_type"],
            )
            self.patch_validator.validate_top_level(graph_patch)
            # Build the new operations before mutating change_set so a model or
            # validation failure does not destroy the existing draft.
            new_operations = self.patch_validator.operations_from_graph_patch(
                change_set,
                graph_patch,
            )
        except GraphDraftingError as exc:
            raise ValidationError(f"Could not revise the draft: {exc}") from exc
        revisions: list[dict[str, Any]] = []
        if isinstance(change_set.context_packet, dict):
            revisions = list(change_set.context_packet.get("reviewer_revisions") or [])
        revisions.append({"feedback": cleaned, "at": utc_now().isoformat()})
        change_set.operations = new_operations
        change_set.summary = str(graph_patch.get("summary") or "")
        change_set.uncertain_fields = string_list(graph_patch.get("uncertain_fields"))
        change_set.clarification_requests = string_list(
            graph_patch.get("clarification_requests")
        )
        change_set.context_packet = context_packet
        if isinstance(change_set.context_packet, dict):
            change_set.context_packet["reviewer_revisions"] = revisions
        change_set.status = GraphChangeSetStatus.READY
        change_set.error_metadata = {}
        change_set.updated_at = utc_now()
        self._save_graph_change_set(change_set)
        return change_set

    @staticmethod
    def _compose_revise_hint(
        operations: list[GraphChangeOperation],
        feedback: str,
    ) -> str:
        lines = []
        for operation in operations:
            semantic = (
                operation.semantic_type.value
                if operation.semantic_type
                else operation.op.value
            )
            try:
                payload_text = json.dumps(operation.payload, default=str)
            except (TypeError, ValueError):
                payload_text = str(operation.payload)
            lines.append(
                f"- [{operation.status.value}] {semantic} "
                f"on {operation.entity_type.value}: {payload_text}"
            )
        prior = "\n".join(lines) if lines else "(none)"
        return (
            "REVISION REQUEST. You previously proposed the graph operations below. "
            "Return a complete, corrected operation set (not a diff) that honors the "
            "reviewer's feedback while staying grounded in the note and graph context."
            f"\n\nPreviously proposed operations:\n{prior}\n\nReviewer feedback: {feedback}"
        )

    def commit_graph_change_set(
        self,
        change_set_id: UUID,
        *,
        message: str,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        if not message or not message.strip():
            raise ValidationError("message must not be empty.")
        change_set = self.get_graph_change_set(change_set_id)
        self.authorization.require_owner(change_set.project_id, actor=actor)
        if change_set.status not in {
            GraphChangeSetStatus.READY,
            GraphChangeSetStatus.SUBMITTED,
        }:
            raise ValidationError("Only ready or submitted graph drafts can be committed.")
        ref_map: dict[str, UUID] = {}
        accepted = [
            operation
            for operation in sorted(change_set.operations, key=lambda item: item.sequence)
            if operation.status == GraphChangeOperationStatus.ACCEPTED
        ]
        if not accepted:
            raise ValidationError("At least one accepted operation is required to commit.")
        for operation in accepted:
            entity = self.patch_applier.apply_graph_operation(
                operation,
                ref_map=ref_map,
                actor=actor,
                change_set=change_set,
            )
            resolved_entity_id = graph_entity_id(operation.entity_type, entity)
            if operation.client_ref:
                ref_map[operation.client_ref] = resolved_entity_id
            operation.status = GraphChangeOperationStatus.APPLIED
            operation.result_entity_id = resolved_entity_id
            operation.error_metadata = {}
            operation.updated_at = utc_now()
        change_set.status = GraphChangeSetStatus.COMMITTED
        change_set.commit_message = message.strip()
        change_set.committed_at = utc_now()
        change_set.committed_by = actor_user_id(actor)
        change_set.updated_at = change_set.committed_at
        self.versions.mark_change_set_committed(
            change_set.change_set_id,
            change_set.committed_at,
        )
        self._save_graph_change_set(change_set)
        return change_set

    def _is_graph_change_set_author(
        self,
        change_set: GraphChangeSet,
        actor: AuthContext | None,
    ) -> bool:
        return actor is not None and change_set.created_by == str(actor.user_id)

    def _ensure_graph_change_set_editable(
        self,
        change_set: GraphChangeSet,
        *,
        actor: AuthContext | None,
    ) -> None:
        if change_set.status in {
            GraphChangeSetStatus.COMMITTED,
            GraphChangeSetStatus.REJECTED,
            GraphChangeSetStatus.FAILED,
        }:
            raise ValidationError("This graph draft cannot be edited.")
        if self.authorization.has_global_write(actor):
            return
        self.authorization.require_contributor(change_set.project_id, actor=actor)
        if not self._is_graph_change_set_author(change_set, actor):
            raise ValidationError("Only the graph draft author can edit this draft.")
        if change_set.status not in {
            GraphChangeSetStatus.READY,
            GraphChangeSetStatus.CHANGES_REQUESTED,
        }:
            raise ValidationError("Submitted graph drafts cannot be edited by contributors.")

    def _save_graph_change_set(self, change_set: GraphChangeSet) -> None:
        change_set.operation_count = len(change_set.operations)
        with self.unit_of_work() as repository:
            repository.graph_change_sets.save(change_set)

    def _find_graph_operation(
        self,
        change_set: GraphChangeSet,
        operation_id: UUID,
    ) -> GraphChangeOperation:
        for operation in change_set.operations:
            if operation.operation_id == operation_id:
                return operation
        raise NotFoundError("Graph draft operation does not exist.")

    def build_graph_context_for_note(
        self,
        note_id: UUID,
        *,
        user_hint: str | None = None,
        actor: AuthContext | None = None,
    ) -> dict[str, Any]:
        prepared = self.context_builder.prepare_note_sources_for_graph_draft(
            note_id,
            mode=GraphDraftMode.GRAPH_CONTEXT,
        )
        return self.context_builder.build_graph_context_packet(
            prepared["source_note"],
            source_notes=prepared["source_notes"],
            user_hint=user_hint.strip() if user_hint else None,
            actor=actor,
        )

    def build_batch_graph_context(
        self,
        notes: list[Note],
        *,
        window: tuple[Any, Any] | None = None,
        actor: AuthContext | None = None,
    ) -> dict[str, Any]:
        return self.context_builder.build_batch_graph_context(
            notes,
            window=window,
            actor=actor,
            batch_note_limit=_BATCH_NOTE_LIMIT,
        )

    def _draft_graph_patch(
        self,
        draft_client: Any,
        *,
        graph_context: dict[str, Any],
        user_hint: str | None,
        draft_mode: GraphDraftMode,
        source_artifacts: list[dict[str, Any]],
        image_bytes: bytes | None,
        image_content_type: str | None,
    ) -> dict[str, Any]:
        draft_from_note = getattr(draft_client, "draft_from_note", None)
        if callable(draft_from_note):
            return draft_from_note(
                graph_context=graph_context,
                user_hint=user_hint,
                draft_mode=draft_mode.value,
                source_artifacts=source_artifacts,
                image_bytes=image_bytes,
                image_content_type=image_content_type,
            )
        draft_from_image = getattr(draft_client, "draft_from_image", None)
        if callable(draft_from_image) and image_bytes and image_content_type:
            return draft_from_image(
                image_bytes=image_bytes,
                content_type=image_content_type,
                graph_context=graph_context,
                user_hint=user_hint,
                draft_mode=draft_mode.value,
            )
        raise GraphDraftingError("Configured draft client does not support this note source.")


def _batch_key(
    *,
    project_id: UUID,
    since: datetime,
    until: datetime,
    note_ids: list[UUID],
) -> str:
    payload = {
        "project_id": str(project_id),
        "since": _as_utc(since).isoformat(),
        "until": _as_utc(until).isoformat(),
        "note_ids": [str(note_id) for note_id in note_ids],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"batch:{digest[:48]}"


def _batch_input_snapshot(context_packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "batch_window": context_packet.get("batch_window"),
        "context_summary": context_packet.get("context_summary"),
        "source_note_ids": [
            item.get("id")
            for item in context_packet.get("batch_notes", [])
            if isinstance(item, dict)
        ],
    }


def _attach_batch_source_traceability(
    operations: list[GraphChangeOperation],
    note_ids: list[UUID],
) -> None:
    note_id_strings = [str(note_id) for note_id in note_ids]
    fallback_ref = {
        "label": "batch source notes",
        "quote": "",
        "region": None,
        "source_note_ids": note_id_strings,
    }
    for operation in operations:
        if not operation.source_refs:
            operation.source_refs = [dict(fallback_ref)]
            continue
        next_refs: list[dict[str, Any]] = []
        for ref in operation.source_refs:
            next_ref = dict(ref)
            if not any(
                key in next_ref for key in ("note_id", "source_note_id", "source_note_ids")
            ):
                next_ref["source_note_ids"] = note_id_strings
            next_refs.append(next_ref)
        operation.source_refs = next_refs


def _default_batch_settings(
    *,
    project_id: UUID,
    actor: AuthContext | None,
) -> GraphDraftBatchSettings:
    return GraphDraftBatchSettings(
        settings_id=uuid4(),
        project_id=project_id,
        enabled=True,
        cadence_minutes=_DEFAULT_BATCH_CADENCE_MINUTES,
        run_at_local_time=_DEFAULT_BATCH_RUN_TIME,
        timezone_name=_DEFAULT_BATCH_TIMEZONE,
        next_run_at=_next_run_at(
            cadence_minutes=_DEFAULT_BATCH_CADENCE_MINUTES,
            run_at_local_time=_DEFAULT_BATCH_RUN_TIME,
            timezone_name=_DEFAULT_BATCH_TIMEZONE,
        ),
        updated_by=actor_user_id(actor),
    )


def _validate_run_at_local_time(value: str) -> None:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValidationError("run_at_local_time must be HH:MM.")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValidationError("run_at_local_time must be HH:MM.") from exc
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValidationError("run_at_local_time must be a valid 24-hour HH:MM time.")


def _zoneinfo(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError(f"Unknown timezone: {timezone_name}") from exc


def _next_run_at(
    *,
    cadence_minutes: int,
    run_at_local_time: str,
    timezone_name: str,
    now: datetime | None = None,
) -> datetime:
    _validate_run_at_local_time(run_at_local_time)
    zone = _zoneinfo(timezone_name)
    current = _as_utc(now or utc_now()).astimezone(zone)
    hour, minute = (int(part) for part in run_at_local_time.split(":"))
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    cadence = timedelta(minutes=cadence_minutes)
    while candidate <= current:
        candidate += cadence
    return candidate.astimezone(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _staged_notes_in_window(
    notes: list[Note],
    *,
    since: datetime,
    until: datetime,
) -> list[Note]:
    start = _as_utc(since)
    end = _as_utc(until)
    return sorted(
        [
            note
            for note in notes
            if note.status == NoteStatus.STAGED and start < _as_utc(note.created_at) <= end
        ],
        key=lambda item: (item.created_at, str(item.note_id)),
    )
