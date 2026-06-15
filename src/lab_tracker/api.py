"""Core API facade and repository wiring for lab tracker."""

from __future__ import annotations

import logging
from collections.abc import Callable
from types import TracebackType
from typing import Any, TypeVar
from uuid import UUID

from lab_tracker.models import Note, Question
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.repository import LabTrackerRepository
from lab_tracker.request_context import LabTrackerRequestContext
from lab_tracker.services import (
    AnalysisService,
    ClaimService,
    DatasetService,
    GoalService,
    GraphDraftService,
    NoteService,
    ProjectAuthorizationPolicy,
    ProjectService,
    QuestionService,
    ServiceContext,
    SessionService,
    SupervisionService,
    VisualizationService,
)

_logger = logging.getLogger(__name__)
ResponseT = TypeVar("ResponseT")


class LabTrackerAPI:
    def __init__(
        self,
        *,
        raw_storage: LocalNoteStorage | None = None,
        repository: LabTrackerRepository | None = None,
        request_context: LabTrackerRequestContext | None = None,
    ) -> None:
        self._raw_storage = raw_storage
        self._repository = repository
        self._request_context = request_context
        self._service_context = ServiceContext(
            raw_storage=raw_storage,
            repository=repository,
            request_context=request_context,
        )
        self._compose_services()

    def _compose_services(self) -> None:
        context = self._service_context
        self.project_authorization: ProjectAuthorizationPolicy = ProjectAuthorizationPolicy(
            context
        )
        self.projects: ProjectService = ProjectService(
            context,
            authorization=self.project_authorization,
        )
        self.supervision: SupervisionService = SupervisionService(context)
        self.questions: QuestionService = QuestionService(
            context,
            projects=self.projects,
            notes_provider=lambda: self.notes,
            authorization=self.project_authorization,
        )
        self.datasets: DatasetService = DatasetService(
            context,
            projects=self.projects,
            questions=self.questions,
            sessions_provider=lambda: self.sessions,
            authorization=self.project_authorization,
        )
        self.sessions: SessionService = SessionService(
            context,
            projects=self.projects,
            questions=self.questions,
            datasets_provider=lambda: self.datasets,
            authorization=self.project_authorization,
        )
        self.analyses: AnalysisService = AnalysisService(
            context,
            projects=self.projects,
            datasets=self.datasets,
            claims_provider=lambda: self.claims,
            visualizations_provider=lambda: self.visualizations,
            authorization=self.project_authorization,
        )
        self.claims: ClaimService = ClaimService(
            context,
            projects=self.projects,
            datasets=self.datasets,
            questions=self.questions,
            analyses_provider=lambda: self.analyses,
            authorization=self.project_authorization,
        )
        self.visualizations: VisualizationService = VisualizationService(
            context,
            analyses=self.analyses,
            claims=self.claims,
            authorization=self.project_authorization,
        )
        self.goals: GoalService = GoalService(
            context,
            projects=self.projects,
            questions=self.questions,
            datasets=self.datasets,
            sessions_provider=lambda: self.sessions,
            analyses_provider=lambda: self.analyses,
            claims_provider=lambda: self.claims,
            visualizations_provider=lambda: self.visualizations,
            notes_provider=lambda: self.notes,
            authorization=self.project_authorization,
        )
        self.notes: NoteService = NoteService(
            context,
            projects=self.projects,
            questions=self.questions,
            datasets=self.datasets,
            sessions=self.sessions,
            analyses=self.analyses,
            claims=self.claims,
            visualizations=self.visualizations,
            goals_provider=lambda: self.goals,
            authorization=self.project_authorization,
        )
        self.graph_drafts: GraphDraftService = GraphDraftService(
            context,
            projects=self.projects,
            questions=self.questions,
            notes=self.notes,
            sessions=self.sessions,
            datasets=self.datasets,
            analyses=self.analyses,
            claims=self.claims,
            visualizations=self.visualizations,
            goals=self.goals,
            authorization=self.project_authorization,
        )

    def for_request(self, repository: LabTrackerRepository) -> LabTrackerAPI:
        return self._for_request_context(LabTrackerRequestContext(repository=repository))

    def _for_request_context(
        self,
        request_context: LabTrackerRequestContext,
    ) -> LabTrackerAPI:
        return self.__class__(
            raw_storage=self._raw_storage,
            repository=request_context.repository,
            request_context=request_context,
        )

    def request_scope(
        self,
        repository: LabTrackerRepository,
        *,
        close: Callable[[], None] | None = None,
    ) -> LabTrackerRequestScope:
        return LabTrackerRequestScope(root_api=self, repository=repository, close=close)

    def _run_deferred_actions(
        self,
        actions: list[Callable[[], None]] | None,
        *,
        label: str,
    ) -> None:
        for action in actions or []:
            try:
                action()
            except Exception as exc:
                # Deferred cleanup should not reverse an already-decided request outcome.
                # Log and continue so independent cleanup actions still get a chance to run.
                _logger.warning("Deferred %s action failed: %s", label, exc, exc_info=True)

    def run_after_commit(self, action: Callable[[], None]) -> None:
        if self._request_context is not None:
            self._request_context.after_commit_actions.append(action)
            return
        action()

    def run_after_rollback(self, action: Callable[[], None]) -> None:
        if self._request_context is None:
            return
        self._request_context.after_rollback_actions.append(action)

    def create_project(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.create_project(*args, **kwargs)

    def get_project(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_project(*args, **kwargs)

    def list_projects(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.list_projects(*args, **kwargs)

    def update_project(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.update_project(*args, **kwargs)

    def delete_project(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.delete_project(*args, **kwargs)

    def create_project_group(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.create_project_group(*args, **kwargs)

    def get_project_group(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_project_group(*args, **kwargs)

    def list_project_groups(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.list_project_groups(*args, **kwargs)

    def update_project_group(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.update_project_group(*args, **kwargs)

    def delete_project_group(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.delete_project_group(*args, **kwargs)

    def get_project_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_project_membership(*args, **kwargs)

    def get_project_membership_for_user(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_project_membership_for_user(*args, **kwargs)

    def list_project_memberships(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.list_project_memberships(*args, **kwargs)

    def upsert_project_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.upsert_project_membership(*args, **kwargs)

    def delete_project_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.delete_project_membership(*args, **kwargs)

    def get_group_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_group_membership(*args, **kwargs)

    def get_group_membership_for_user(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_group_membership_for_user(*args, **kwargs)

    def list_group_memberships(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.list_group_memberships(*args, **kwargs)

    def upsert_group_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.upsert_group_membership(*args, **kwargs)

    def delete_group_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.delete_group_membership(*args, **kwargs)

    def upsert_group_project_memberships(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.upsert_group_project_memberships(*args, **kwargs)

    def delete_group_project_memberships(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.delete_group_project_memberships(*args, **kwargs)

    def accessible_project_ids(self, *args: Any, **kwargs: Any) -> Any:
        return self.project_authorization.accessible_project_ids(*args, **kwargs)

    def create_supervision_edge(self, *args: Any, **kwargs: Any) -> Any:
        return self.supervision.create_supervision_edge(*args, **kwargs)

    def get_supervision_edge(self, *args: Any, **kwargs: Any) -> Any:
        return self.supervision.get_supervision_edge(*args, **kwargs)

    def list_supervision_edges(self, *args: Any, **kwargs: Any) -> Any:
        return self.supervision.list_supervision_edges(*args, **kwargs)

    def update_supervision_edge(self, *args: Any, **kwargs: Any) -> Any:
        return self.supervision.update_supervision_edge(*args, **kwargs)

    def delete_supervision_edge(self, *args: Any, **kwargs: Any) -> Any:
        return self.supervision.delete_supervision_edge(*args, **kwargs)

    def project_membership_role(self, *args: Any, **kwargs: Any) -> Any:
        return self.project_authorization.membership_role(*args, **kwargs)

    def require_project_read(self, *args: Any, **kwargs: Any) -> Any:
        return self.project_authorization.require_read(*args, **kwargs)

    def require_project_contributor(self, *args: Any, **kwargs: Any) -> Any:
        return self.project_authorization.require_contributor(*args, **kwargs)

    def require_project_owner(self, *args: Any, **kwargs: Any) -> Any:
        return self.project_authorization.require_owner(*args, **kwargs)

    def group_membership_role(self, *args: Any, **kwargs: Any) -> Any:
        return self.project_authorization.group_membership_role(*args, **kwargs)

    def require_group_read(self, *args: Any, **kwargs: Any) -> Any:
        return self.project_authorization.require_group_read(*args, **kwargs)

    def require_group_owner(self, *args: Any, **kwargs: Any) -> Any:
        return self.project_authorization.require_group_owner(*args, **kwargs)

    def create_question(self, *args: Any, **kwargs: Any) -> Any:
        return self.questions.create_question(*args, **kwargs)

    def get_question(self, *args: Any, **kwargs: Any) -> Any:
        return self.questions.get_question(*args, **kwargs)

    def list_questions(self, *args: Any, **kwargs: Any) -> Any:
        return self.questions.list_questions(*args, **kwargs)

    def list_questions_filtered(self, *args: Any, **kwargs: Any) -> Any:
        return self.questions.list_questions_filtered(*args, **kwargs)

    def update_question(self, *args: Any, **kwargs: Any) -> Any:
        return self.questions.update_question(*args, **kwargs)

    def list_question_refactors(self, *args: Any, **kwargs: Any) -> Any:
        return self.questions.list_question_refactors(*args, **kwargs)

    def refactor_question(self, *args: Any, **kwargs: Any) -> Any:
        return self.questions.refactor_question(*args, **kwargs)

    def delete_question(self, *args: Any, **kwargs: Any) -> Any:
        return self.questions.delete_question(*args, **kwargs)

    def create_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self.datasets.create_dataset(*args, **kwargs)

    def get_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self.datasets.get_dataset(*args, **kwargs)

    def list_datasets(self, *args: Any, **kwargs: Any) -> Any:
        return self.datasets.list_datasets(*args, **kwargs)

    def update_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self.datasets.update_dataset(*args, **kwargs)

    def delete_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self.datasets.delete_dataset(*args, **kwargs)

    def create_session(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.create_session(*args, **kwargs)

    def get_session(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.get_session(*args, **kwargs)

    def get_session_by_link_code(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.get_session_by_link_code(*args, **kwargs)

    def list_sessions(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.list_sessions(*args, **kwargs)

    def update_session(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.update_session(*args, **kwargs)

    def delete_session(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.delete_session(*args, **kwargs)

    def register_acquisition_output(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.register_acquisition_output(*args, **kwargs)

    def list_acquisition_outputs(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.list_acquisition_outputs(*args, **kwargs)

    def delete_acquisition_output(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.delete_acquisition_output(*args, **kwargs)

    def promote_operational_session(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.promote_operational_session(*args, **kwargs)

    def promote_operational_session_to_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.promote_operational_session_to_dataset(*args, **kwargs)

    def create_note(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.create_note(*args, **kwargs)

    def store_note_raw_asset(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.store_note_raw_asset(*args, **kwargs)

    def upload_note_raw(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.upload_note_raw(*args, **kwargs)

    def transcribe_voice_note(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.transcribe_voice_note(*args, **kwargs)

    def get_note(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.get_note(*args, **kwargs)

    def list_notes(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.list_notes(*args, **kwargs)

    def update_note(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.update_note(*args, **kwargs)

    def download_note_raw(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.download_note_raw(*args, **kwargs)

    def delete_note(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.delete_note(*args, **kwargs)

    def create_analysis(self, *args: Any, **kwargs: Any) -> Any:
        return self.analyses.create_analysis(*args, **kwargs)

    def get_analysis(self, *args: Any, **kwargs: Any) -> Any:
        return self.analyses.get_analysis(*args, **kwargs)

    def list_analyses(self, *args: Any, **kwargs: Any) -> Any:
        return self.analyses.list_analyses(*args, **kwargs)

    def update_analysis(self, *args: Any, **kwargs: Any) -> Any:
        return self.analyses.update_analysis(*args, **kwargs)

    def delete_analysis(self, *args: Any, **kwargs: Any) -> Any:
        return self.analyses.delete_analysis(*args, **kwargs)

    def commit_analysis(self, *args: Any, **kwargs: Any) -> Any:
        return self.analyses.commit_analysis(*args, **kwargs)

    def create_claim(self, *args: Any, **kwargs: Any) -> Any:
        return self.claims.create_claim(*args, **kwargs)

    def get_claim(self, *args: Any, **kwargs: Any) -> Any:
        return self.claims.get_claim(*args, **kwargs)

    def list_claims(self, *args: Any, **kwargs: Any) -> Any:
        return self.claims.list_claims(*args, **kwargs)

    def update_claim(self, *args: Any, **kwargs: Any) -> Any:
        return self.claims.update_claim(*args, **kwargs)

    def delete_claim(self, *args: Any, **kwargs: Any) -> Any:
        return self.claims.delete_claim(*args, **kwargs)

    def create_goal(self, *args: Any, **kwargs: Any) -> Any:
        return self.goals.create_goal(*args, **kwargs)

    def get_goal(self, *args: Any, **kwargs: Any) -> Any:
        return self.goals.get_goal(*args, **kwargs)

    def list_goals(self, *args: Any, **kwargs: Any) -> Any:
        return self.goals.list_goals(*args, **kwargs)

    def update_goal(self, *args: Any, **kwargs: Any) -> Any:
        return self.goals.update_goal(*args, **kwargs)

    def delete_goal(self, *args: Any, **kwargs: Any) -> Any:
        return self.goals.delete_goal(*args, **kwargs)

    def link_node_to_goal(self, *args: Any, **kwargs: Any) -> Any:
        return self.goals.link_node_to_goal(*args, **kwargs)

    def update_goal_link(self, *args: Any, **kwargs: Any) -> Any:
        return self.goals.update_goal_link(*args, **kwargs)

    def delete_goal_link(self, *args: Any, **kwargs: Any) -> Any:
        return self.goals.delete_goal_link(*args, **kwargs)

    def list_node_goals(self, *args: Any, **kwargs: Any) -> Any:
        return self.goals.list_node_goals(*args, **kwargs)

    def create_visualization(self, *args: Any, **kwargs: Any) -> Any:
        return self.visualizations.create_visualization(*args, **kwargs)

    def get_visualization(self, *args: Any, **kwargs: Any) -> Any:
        return self.visualizations.get_visualization(*args, **kwargs)

    def list_visualizations(self, *args: Any, **kwargs: Any) -> Any:
        return self.visualizations.list_visualizations(*args, **kwargs)

    def update_visualization(self, *args: Any, **kwargs: Any) -> Any:
        return self.visualizations.update_visualization(*args, **kwargs)

    def delete_visualization(self, *args: Any, **kwargs: Any) -> Any:
        return self.visualizations.delete_visualization(*args, **kwargs)

    def create_graph_draft_from_note(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.create_graph_draft_from_note(*args, **kwargs)

    def create_batch_graph_draft(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.create_batch_graph_draft(*args, **kwargs)

    def get_graph_change_set(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.get_graph_change_set(*args, **kwargs)

    def list_graph_change_sets(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.list_graph_change_sets(*args, **kwargs)

    def list_batch_graph_drafts(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.list_batch_graph_drafts(*args, **kwargs)

    def update_graph_change_operation(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.update_graph_change_operation(*args, **kwargs)

    def submit_graph_change_set(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.submit_graph_change_set(*args, **kwargs)

    def review_graph_change_set(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.review_graph_change_set(*args, **kwargs)

    def revise_graph_change_set(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.revise_graph_change_set(*args, **kwargs)

    def commit_graph_change_set(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.commit_graph_change_set(*args, **kwargs)

    def build_graph_context_for_note(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.build_graph_context_for_note(*args, **kwargs)

    def build_batch_graph_context(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.build_batch_graph_context(*args, **kwargs)

    def get_graph_draft_batch_settings(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.get_graph_draft_batch_settings(*args, **kwargs)

    def update_graph_draft_batch_settings(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.update_graph_draft_batch_settings(*args, **kwargs)

    def run_graph_draft_batch_for_project(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.run_graph_draft_batch_for_project(*args, **kwargs)

    def run_due_graph_draft_batches(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.run_due_graph_draft_batches(*args, **kwargs)

    def list_graph_draft_batch_runs(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.list_graph_draft_batch_runs(*args, **kwargs)

    def search_questions(
        self,
        query: str,
        *,
        project_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Question]:
        repository = self._service_context.active_repository()
        questions, _ = repository.query_questions(
            project_id=project_id,
            search=query,
            limit=limit,
            offset=offset,
        )
        return questions

    def search_notes(
        self,
        query: str,
        *,
        project_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Note]:
        repository = self._service_context.active_repository()
        notes, _ = repository.query_notes(
            project_id=project_id,
            search=query,
            limit=limit,
            offset=offset,
        )
        return notes


class LabTrackerRequestScope:
    def __init__(
        self,
        *,
        root_api: LabTrackerAPI,
        repository: LabTrackerRepository,
        close: Callable[[], None] | None = None,
    ) -> None:
        self._root_api = root_api
        self._context = LabTrackerRequestContext(repository=repository)
        self._close = close
        self._completed = False
        self.api = root_api._for_request_context(self._context)

    def __enter__(self) -> LabTrackerRequestScope:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if not self._completed:
                self.rollback()
        finally:
            if self._close is not None:
                self._close()

    def complete_response(self, response: ResponseT) -> ResponseT:
        if response.status_code >= 400:
            self.rollback()
        else:
            self.commit()
        return response

    def commit(self) -> None:
        if self._completed:
            return
        try:
            self._context.repository.commit()
        except Exception:
            self._context.repository.rollback()
            self._complete_rollback()
            raise
        self._complete_commit()

    def rollback(self) -> None:
        if self._completed:
            return
        try:
            self._context.repository.rollback()
        finally:
            self._complete_rollback()

    def _complete_commit(self) -> None:
        self._completed = True
        self._context.complete_commit(
            run_deferred_actions=lambda actions, label: self._root_api._run_deferred_actions(
                actions,
                label=label,
            ),
        )

    def _complete_rollback(self) -> None:
        self._completed = True
        self._context.complete_rollback(
            run_deferred_actions=lambda actions, label: self._root_api._run_deferred_actions(
                actions,
                label=label,
            ),
        )
