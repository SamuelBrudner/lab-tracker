"""Core API facade and repository wiring for lab tracker."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any, TypeVar
from uuid import UUID

from lab_tracker.config import Settings, get_settings
from lab_tracker.models import (
    Note,
    Question,
    UsageEventOutcome,
    UsageEventResourceType,
    UsageEventVerb,
)
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.repository import LabTrackerRepository
from lab_tracker.request_context import LabTrackerRequestContext
from lab_tracker.services import (
    AnalysisService,
    ClaimService,
    DatasetService,
    EntityVersionService,
    ExplorationService,
    GoalService,
    GraphDraftService,
    NoteService,
    OwnershipReassignmentService,
    ProjectAuthorizationPolicy,
    ProjectService,
    ProvenanceLinkService,
    PublicationReadinessService,
    QuestionService,
    RecordExportService,
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
        settings: Settings | None = None,
        surface: str | None = None,
    ) -> None:
        self._raw_storage = raw_storage
        self._repository = repository
        self._request_context = request_context
        self._settings = settings or get_settings()
        self._surface = surface
        self._service_context = ServiceContext(
            raw_storage=raw_storage,
            repository=repository,
            request_context=request_context,
            settings=self._settings,
            surface=surface,
        )
        self._compose_services()

    def _compose_services(self) -> None:
        context = self._service_context
        self.project_authorization: ProjectAuthorizationPolicy = ProjectAuthorizationPolicy(context)
        self.projects: ProjectService = ProjectService(
            context,
            authorization=self.project_authorization,
        )
        self.supervision: SupervisionService = SupervisionService(context)
        self.ownership_reassignments: OwnershipReassignmentService = OwnershipReassignmentService(
            context
        )
        self.record_exports: RecordExportService = RecordExportService(
            context,
            authorization=self.project_authorization,
        )
        self.publication_readiness: PublicationReadinessService = PublicationReadinessService(
            context,
            projects=self.projects,
            authorization=self.project_authorization,
        )
        self.entity_versions: EntityVersionService = EntityVersionService(context)
        self.questions: QuestionService = QuestionService(
            context,
            projects=self.projects,
            notes_provider=lambda: self.notes,
            versions=self.entity_versions,
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
            versions=self.entity_versions,
            authorization=self.project_authorization,
        )
        self.exploration: ExplorationService = ExplorationService(
            context,
            authorization=self.project_authorization,
        )
        self.provenance_links: ProvenanceLinkService = ProvenanceLinkService(
            context,
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
            versions=self.entity_versions,
            authorization=self.project_authorization,
            provenance_links=self.provenance_links,
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
            settings=self._settings,
        )

    def request_scope(
        self,
        repository: LabTrackerRepository,
        *,
        surface: str | None = None,
        close: Callable[[], None] | None = None,
    ) -> LabTrackerRequestScope:
        return LabTrackerRequestScope(
            root_api=self,
            repository=repository,
            surface=surface,
            close=close,
        )

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

    def record_usage_event(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.record_usage_event(*args, **kwargs)

    def _with_usage_event(
        self,
        action: Callable[[], Any],
        *,
        verb: UsageEventVerb,
        resource_type: UsageEventResourceType,
        actor: Any = None,
        resource_id: UUID | None = None,
        project_id: UUID | None = None,
        resource_id_attr: str | None = None,
        project_id_attr: str | None = "project_id",
    ) -> Any:
        start = time.perf_counter()
        try:
            result = action()
        except Exception:
            self.record_usage_event(
                verb=verb,
                resource_type=resource_type,
                resource_id=resource_id,
                project_id=project_id,
                actor=actor,
                outcome=UsageEventOutcome.ERROR,
                duration_ms=_elapsed_ms(start),
            )
            raise
        self.record_usage_event(
            verb=verb,
            resource_type=resource_type,
            resource_id=resource_id or _uuid_attr(result, resource_id_attr),
            project_id=project_id or _uuid_attr(result, project_id_attr),
            actor=actor,
            duration_ms=_elapsed_ms(start),
        )
        return result

    def create_project(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.create_project(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.PROJECT,
            actor=kwargs.get("actor"),
            resource_id_attr="project_id",
            project_id_attr="project_id",
        )

    def get_project(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_project(*args, **kwargs)

    def list_projects(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.list_projects(*args, **kwargs)

    def update_project(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.update_project(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.PROJECT,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            project_id=_first_uuid(args),
            resource_id_attr="project_id",
            project_id_attr="project_id",
        )

    def delete_project(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.delete_project(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.PROJECT,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            project_id=_first_uuid(args),
            resource_id_attr="project_id",
            project_id_attr="project_id",
        )

    def create_project_group(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.create_project_group(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.PROJECT_GROUP,
            actor=kwargs.get("actor"),
            resource_id_attr="group_id",
            project_id_attr=None,
        )

    def get_project_group(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_project_group(*args, **kwargs)

    def list_project_groups(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.list_project_groups(*args, **kwargs)

    def update_project_group(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.update_project_group(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.PROJECT_GROUP,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="group_id",
            project_id_attr=None,
        )

    def delete_project_group(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.delete_project_group(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.PROJECT_GROUP,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="group_id",
            project_id_attr=None,
        )

    def get_project_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_project_membership(*args, **kwargs)

    def get_project_membership_for_user(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_project_membership_for_user(*args, **kwargs)

    def list_project_memberships(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.list_project_memberships(*args, **kwargs)

    def upsert_project_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.upsert_project_membership(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.PROJECT_MEMBERSHIP,
            actor=kwargs.get("actor"),
            resource_id_attr="membership_id",
        )

    def update_project_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.update_project_membership(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.PROJECT_MEMBERSHIP,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="membership_id",
        )

    def delete_project_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.delete_project_membership(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.PROJECT_MEMBERSHIP,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="membership_id",
        )

    def get_group_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_group_membership(*args, **kwargs)

    def get_group_membership_for_user(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_group_membership_for_user(*args, **kwargs)

    def list_group_memberships(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.list_group_memberships(*args, **kwargs)

    def upsert_group_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.upsert_group_membership(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.GROUP_MEMBERSHIP,
            actor=kwargs.get("actor"),
            resource_id_attr="membership_id",
            project_id_attr=None,
        )

    def delete_group_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.delete_group_membership(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.GROUP_MEMBERSHIP,
            actor=kwargs.get("actor"),
            resource_id_attr="membership_id",
            project_id_attr=None,
        )

    def upsert_group_project_memberships(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.upsert_group_project_memberships(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.PROJECT_MEMBERSHIP,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            project_id_attr=None,
        )

    def delete_group_project_memberships(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.delete_group_project_memberships(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.PROJECT_MEMBERSHIP,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            project_id_attr=None,
        )

    def accessible_project_ids(self, *args: Any, **kwargs: Any) -> Any:
        return self.project_authorization.accessible_project_ids(*args, **kwargs)

    def create_supervision_edge(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.supervision.create_supervision_edge(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.SUPERVISION_EDGE,
            actor=kwargs.get("actor"),
            resource_id_attr="edge_id",
            project_id_attr=None,
        )

    def get_supervision_edge(self, *args: Any, **kwargs: Any) -> Any:
        return self.supervision.get_supervision_edge(*args, **kwargs)

    def list_supervision_edges(self, *args: Any, **kwargs: Any) -> Any:
        return self.supervision.list_supervision_edges(*args, **kwargs)

    def update_supervision_edge(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.supervision.update_supervision_edge(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.SUPERVISION_EDGE,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="edge_id",
            project_id_attr=None,
        )

    def delete_supervision_edge(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.supervision.delete_supervision_edge(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.SUPERVISION_EDGE,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="edge_id",
            project_id_attr=None,
        )

    def reassign_ownership(self, *args: Any, **kwargs: Any) -> Any:
        return self.ownership_reassignments.reassign_ownership(*args, **kwargs)

    def get_ownership_reassignment(self, *args: Any, **kwargs: Any) -> Any:
        return self.ownership_reassignments.get_ownership_reassignment(*args, **kwargs)

    def list_ownership_reassignments(self, *args: Any, **kwargs: Any) -> Any:
        return self.ownership_reassignments.list_ownership_reassignments(
            *args,
            **kwargs,
        )

    def export_user_records(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.record_exports.export_user_records(*args, **kwargs),
            verb=UsageEventVerb.EXPORT,
            resource_type=UsageEventResourceType.RECORD_EXPORT,
            actor=kwargs.get("actor"),
            project_id=None,
            project_id_attr=None,
        )

    def export_goal_artifact(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.record_exports.export_goal_artifact(*args, **kwargs),
            verb=UsageEventVerb.EXPORT,
            resource_type=UsageEventResourceType.GOAL,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args) or kwargs.get("goal_id"),
            project_id_attr="project_id",
        )

    def export_question_subtree(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.record_exports.export_question_subtree(*args, **kwargs),
            verb=UsageEventVerb.EXPORT,
            resource_type=UsageEventResourceType.QUESTION,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args) or kwargs.get("question_id"),
            project_id_attr="project_id",
        )

    def check_publication_readiness(self, *args: Any, **kwargs: Any) -> Any:
        return self.publication_readiness.check(*args, **kwargs)

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
        return self._with_usage_event(
            lambda: self.questions.create_question(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.QUESTION,
            actor=kwargs.get("actor"),
            resource_id_attr="question_id",
        )

    def get_question(self, *args: Any, **kwargs: Any) -> Any:
        return self.questions.get_question(*args, **kwargs)

    def list_questions(self, *args: Any, **kwargs: Any) -> Any:
        return self.questions.list_questions(*args, **kwargs)

    def list_questions_filtered(self, *args: Any, **kwargs: Any) -> Any:
        return self.questions.list_questions_filtered(*args, **kwargs)

    def update_question(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.questions.update_question(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.QUESTION,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="question_id",
        )

    def list_question_refactors(self, *args: Any, **kwargs: Any) -> Any:
        return self.questions.list_question_refactors(*args, **kwargs)

    def refactor_question(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.questions.refactor_question(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.QUESTION_REFACTOR,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            project_id_attr=None,
        )

    def delete_question(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.questions.delete_question(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.QUESTION,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="question_id",
        )

    def list_entity_versions(self, *args: Any, **kwargs: Any) -> Any:
        return self.entity_versions.list_entity_versions(*args, **kwargs)

    def diff_entity_versions(self, *args: Any, **kwargs: Any) -> Any:
        return self.entity_versions.diff_entity_versions(*args, **kwargs)

    def create_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.datasets.create_dataset(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.DATASET,
            actor=kwargs.get("actor"),
            resource_id_attr="dataset_id",
        )

    def get_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self.datasets.get_dataset(*args, **kwargs)

    def list_datasets(self, *args: Any, **kwargs: Any) -> Any:
        return self.datasets.list_datasets(*args, **kwargs)

    def update_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.datasets.update_dataset(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.DATASET,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="dataset_id",
        )

    def delete_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.datasets.delete_dataset(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.DATASET,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="dataset_id",
        )

    def create_session(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.sessions.create_session(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.SESSION,
            actor=kwargs.get("actor"),
            resource_id_attr="session_id",
        )

    def get_session(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.get_session(*args, **kwargs)

    def get_session_by_link_code(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.get_session_by_link_code(*args, **kwargs)

    def list_sessions(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.list_sessions(*args, **kwargs)

    def update_session(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.sessions.update_session(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.SESSION,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="session_id",
        )

    def delete_session(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.sessions.delete_session(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.SESSION,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="session_id",
        )

    def register_acquisition_output(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.sessions.register_acquisition_output(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.ACQUISITION_OUTPUT,
            actor=kwargs.get("actor"),
            resource_id_attr="output_id",
            project_id_attr=None,
        )

    def list_acquisition_outputs(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.list_acquisition_outputs(*args, **kwargs)

    def delete_acquisition_output(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.sessions.delete_acquisition_output(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.ACQUISITION_OUTPUT,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="output_id",
            project_id_attr=None,
        )

    def promote_operational_session(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.sessions.promote_operational_session(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.SESSION,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="session_id",
        )

    def promote_operational_session_to_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.sessions.promote_operational_session_to_dataset(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.DATASET,
            actor=kwargs.get("actor"),
            resource_id_attr="dataset_id",
        )

    def create_note(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.notes.create_note(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.NOTE,
            actor=kwargs.get("actor"),
            resource_id_attr="note_id",
        )

    def store_note_raw_asset(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.store_note_raw_asset(*args, **kwargs)

    def find_note_by_client_capture_id(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.find_note_by_client_capture_id(*args, **kwargs)

    def upload_note_raw(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.notes.upload_note_raw(*args, **kwargs),
            verb=UsageEventVerb.UPLOAD,
            resource_type=UsageEventResourceType.NOTE,
            actor=kwargs.get("actor"),
            resource_id_attr="note_id",
        )

    def transcribe_voice_note(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.notes.transcribe_voice_note(*args, **kwargs),
            verb=UsageEventVerb.TRANSCRIBE,
            resource_type=UsageEventResourceType.NOTE,
            actor=kwargs.get("actor"),
            resource_id_attr="note_id",
        )

    def get_note(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.get_note(*args, **kwargs)

    def list_notes(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.list_notes(*args, **kwargs)

    def update_note(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.notes.update_note(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.NOTE,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="note_id",
        )

    def archive_note(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.notes.archive_note(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.NOTE,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="note_id",
        )

    def download_note_raw(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.download_note_raw(*args, **kwargs)

    def delete_note(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.notes.delete_note(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.NOTE,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="note_id",
        )

    def create_analysis(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.analyses.create_analysis(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.ANALYSIS,
            actor=kwargs.get("actor"),
            resource_id_attr="analysis_id",
        )

    def get_analysis(self, *args: Any, **kwargs: Any) -> Any:
        return self.analyses.get_analysis(*args, **kwargs)

    def list_analyses(self, *args: Any, **kwargs: Any) -> Any:
        return self.analyses.list_analyses(*args, **kwargs)

    def update_analysis(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.analyses.update_analysis(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.ANALYSIS,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="analysis_id",
        )

    def delete_analysis(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.analyses.delete_analysis(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.ANALYSIS,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="analysis_id",
        )

    def commit_analysis(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.analyses.commit_analysis(*args, **kwargs),
            verb=UsageEventVerb.COMMIT,
            resource_type=UsageEventResourceType.ANALYSIS,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="analysis_id",
        )

    def create_claim(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.claims.create_claim(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.CLAIM,
            actor=kwargs.get("actor"),
            resource_id_attr="claim_id",
        )

    def get_claim(self, *args: Any, **kwargs: Any) -> Any:
        return self.claims.get_claim(*args, **kwargs)

    def list_claims(self, *args: Any, **kwargs: Any) -> Any:
        return self.claims.list_claims(*args, **kwargs)

    def update_claim(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.claims.update_claim(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.CLAIM,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="claim_id",
        )

    def delete_claim(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.claims.delete_claim(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.CLAIM,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="claim_id",
        )

    def create_exploration_node(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.exploration.create_exploration_node(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.EXPLORATION_NODE,
            actor=kwargs.get("actor"),
            resource_id_attr="node_id",
        )

    def get_exploration_node(self, *args: Any, **kwargs: Any) -> Any:
        return self.exploration.get_exploration_node(*args, **kwargs)

    def list_exploration_nodes(self, *args: Any, **kwargs: Any) -> Any:
        return self.exploration.list_exploration_nodes(*args, **kwargs)

    def update_exploration_node(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.exploration.update_exploration_node(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.EXPLORATION_NODE,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="node_id",
        )

    def delete_exploration_node(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.exploration.delete_exploration_node(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.EXPLORATION_NODE,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="node_id",
        )

    def get_provenance_link(self, *args: Any, **kwargs: Any) -> Any:
        return self.provenance_links.get_provenance_link(*args, **kwargs)

    def list_provenance_links(self, *args: Any, **kwargs: Any) -> Any:
        return self.provenance_links.list_provenance_links(*args, **kwargs)

    def update_provenance_link_status(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.provenance_links.update_status(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.PROVENANCE_LINK,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="link_id",
        )

    def create_claim_edge(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.claims.create_claim_edge(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.CLAIM_EDGE,
            actor=kwargs.get("actor"),
            resource_id_attr="edge_id",
            project_id_attr=None,
        )

    def list_claim_edges(self, *args: Any, **kwargs: Any) -> Any:
        return self.claims.list_claim_edges(*args, **kwargs)

    def create_goal(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.goals.create_goal(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GOAL,
            actor=kwargs.get("actor"),
            resource_id_attr="goal_id",
        )

    def get_goal(self, *args: Any, **kwargs: Any) -> Any:
        return self.goals.get_goal(*args, **kwargs)

    def list_goals(self, *args: Any, **kwargs: Any) -> Any:
        return self.goals.list_goals(*args, **kwargs)

    def update_goal(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.goals.update_goal(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.GOAL,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="goal_id",
        )

    def delete_goal(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.goals.delete_goal(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.GOAL,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="goal_id",
        )

    def link_node_to_goal(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.goals.link_node_to_goal(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GOAL_LINK,
            actor=kwargs.get("actor"),
            resource_id_attr="link_id",
            project_id_attr=None,
        )

    def update_goal_link(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.goals.update_goal_link(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.GOAL_LINK,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="link_id",
            project_id_attr=None,
        )

    def delete_goal_link(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.goals.delete_goal_link(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.GOAL_LINK,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="link_id",
            project_id_attr=None,
        )

    def list_node_goals(self, *args: Any, **kwargs: Any) -> Any:
        return self.goals.list_node_goals(*args, **kwargs)

    def create_visualization(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.visualizations.create_visualization(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.VISUALIZATION,
            actor=kwargs.get("actor"),
            resource_id_attr="viz_id",
        )

    def get_visualization(self, *args: Any, **kwargs: Any) -> Any:
        return self.visualizations.get_visualization(*args, **kwargs)

    def list_visualizations(self, *args: Any, **kwargs: Any) -> Any:
        return self.visualizations.list_visualizations(*args, **kwargs)

    def update_visualization(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.visualizations.update_visualization(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.VISUALIZATION,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="viz_id",
        )

    def delete_visualization(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.visualizations.delete_visualization(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.VISUALIZATION,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="viz_id",
        )

    def create_graph_draft_from_note(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.create_graph_draft_from_note(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=kwargs.get("actor"),
            resource_id_attr="change_set_id",
        )

    def create_analysis_graph_draft_from_note(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.create_analysis_graph_draft_from_note(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=kwargs.get("actor"),
            resource_id_attr="change_set_id",
        )

    def create_batch_graph_draft(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.create_batch_graph_draft(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=kwargs.get("actor"),
            resource_id_attr="change_set_id",
        )

    def get_graph_change_set(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.get_graph_change_set(*args, **kwargs)

    def list_graph_change_sets(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.list_graph_change_sets(*args, **kwargs)

    def query_graph_change_sets(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.query_graph_change_sets(*args, **kwargs)

    def list_batch_graph_drafts(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.list_batch_graph_drafts(*args, **kwargs)

    def update_graph_change_operation(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.update_graph_change_operation(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            project_id_attr=None,
        )

    def bulk_accept_graph_change_operations(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.bulk_accept_graph_change_operations(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="change_set_id",
        )

    def submit_graph_change_set(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.submit_graph_change_set(*args, **kwargs),
            verb=UsageEventVerb.SUBMIT,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="change_set_id",
        )

    def review_graph_change_set(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.review_graph_change_set(*args, **kwargs),
            verb=UsageEventVerb.REVIEW,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="change_set_id",
        )

    def revise_graph_change_set(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.revise_graph_change_set(*args, **kwargs)

    def commit_graph_change_set(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.commit_graph_change_set(*args, **kwargs),
            verb=UsageEventVerb.COMMIT,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="change_set_id",
        )

    def build_graph_context_for_note(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.build_graph_context_for_note(*args, **kwargs)

    def build_batch_graph_context(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.build_batch_graph_context(*args, **kwargs)

    def get_graph_draft_batch_settings(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.get_graph_draft_batch_settings(*args, **kwargs)

    def update_graph_draft_batch_settings(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.update_graph_draft_batch_settings(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.GRAPH_DRAFT_BATCH_SETTINGS,
            actor=kwargs.get("actor"),
            resource_id_attr="settings_id",
        )

    def run_graph_draft_batch_for_project(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.run_graph_draft_batch_for_project(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GRAPH_DRAFT_BATCH_RUN,
            actor=kwargs.get("actor"),
            resource_id_attr="run_id",
        )

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

    def query_usage_events(self, *args: Any, **kwargs: Any) -> Any:
        repository = self._service_context.active_repository()
        return repository.query_usage_events(*args, **kwargs)

    def usage_event_summary(self, *args: Any, **kwargs: Any) -> Any:
        repository = self._service_context.active_repository()
        return repository.usage_event_summary(*args, **kwargs)

    def rollup_usage_events_before(self, *args: Any, **kwargs: Any) -> Any:
        repository = self._service_context.active_repository()
        return repository.rollup_usage_events_before(*args, **kwargs)


def _elapsed_ms(start: float) -> int:
    return max(0, round((time.perf_counter() - start) * 1000))


def _first_uuid(values: tuple[Any, ...]) -> UUID | None:
    for value in values:
        if isinstance(value, UUID):
            return value
    return None


def _uuid_attr(value: Any, attr: str | None) -> UUID | None:
    if attr is None:
        return None
    candidate = getattr(value, attr, None)
    return candidate if isinstance(candidate, UUID) else None


class LabTrackerRequestScope:
    def __init__(
        self,
        *,
        root_api: LabTrackerAPI,
        repository: LabTrackerRepository,
        surface: str | None = None,
        close: Callable[[], None] | None = None,
    ) -> None:
        self._root_api = root_api
        self._context = LabTrackerRequestContext(repository=repository, surface=surface)
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
