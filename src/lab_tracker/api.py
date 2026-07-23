"""Core API facade and repository wiring for lab tracker."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any, TypeVar
from uuid import UUID

from lab_tracker.api_parts import (
    AnalysesApiMixin,
    DatasetsApiMixin,
    EvidenceBundlesApiMixin,
    ExplorationApiMixin,
    GoalsApiMixin,
    GraphDraftsApiMixin,
    NotesApiMixin,
    ProjectsApiMixin,
    ProvenanceApiMixin,
    QuestionsApiMixin,
    SessionsApiMixin,
    UsageApiMixin,
)
from lab_tracker.api_parts._base import _elapsed_ms, _uuid_attr
from lab_tracker.config import Settings, get_settings
from lab_tracker.models import (
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
    DataStoreService,
    EntityVersionService,
    EvidenceBundleService,
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


class LabTrackerAPI(
    ProjectsApiMixin,
    QuestionsApiMixin,
    NotesApiMixin,
    DatasetsApiMixin,
    EvidenceBundlesApiMixin,
    SessionsApiMixin,
    AnalysesApiMixin,
    GoalsApiMixin,
    GraphDraftsApiMixin,
    ExplorationApiMixin,
    ProvenanceApiMixin,
    UsageApiMixin,
):
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
        self.data_stores: DataStoreService = DataStoreService(
            context,
            projects=self.projects,
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
        self.evidence_bundles: EvidenceBundleService = EvidenceBundleService(
            context,
            projects=self.projects,
            questions=self.questions,
            datasets=self.datasets,
            analyses=self.analyses,
            claims=self.claims,
            visualizations=self.visualizations,
            notes=self.notes,
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
        with self._service_context.application_transaction():
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
