"""Analysis domain service."""

from __future__ import annotations

from typing import Callable, Iterable, TYPE_CHECKING
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    Analysis,
    AnalysisStatus,
    Claim,
    ClaimInput,
    DatasetStatus,
    Visualization,
    VisualizationInput,
    utc_now,
)
from lab_tracker.services.shared import (
    WRITE_ROLES,
    _analysis_has_question_link,
    _actor_user_id,
    _ensure_analysis_status_transition,
    _ensure_non_empty,
    _unique_ids,
)
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.dataset_service import DatasetService
from lab_tracker.services.project_service import ProjectService

if TYPE_CHECKING:
    from lab_tracker.services.claim_service import ClaimService
    from lab_tracker.services.visualization_service import VisualizationService


class AnalysisService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        datasets: DatasetService,
        claims_provider: Callable[[], "ClaimService"],
        visualizations_provider: Callable[[], "VisualizationService"],
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.datasets = datasets
        self._claims_provider = claims_provider
        self._visualizations_provider = visualizations_provider

    @property
    def claims(self) -> "ClaimService":
        return self._claims_provider()

    @property
    def visualizations(self) -> "VisualizationService":
        return self._visualizations_provider()

    def create_analysis(
        self,
        project_id: UUID,
        dataset_ids: Iterable[UUID],
        method_hash: str,
        code_version: str,
        *,
        environment_hash: str | None = None,
        status: AnalysisStatus = AnalysisStatus.STAGED,
        actor: AuthContext | None = None,
    ) -> Analysis:
        require_role(actor, WRITE_ROLES)
        self.projects.get_project(project_id)
        dataset_id_list = _unique_ids(dataset_ids)
        if not dataset_id_list:
            raise ValidationError("Analysis must reference at least one dataset.")
        datasets = []
        for dataset_id in dataset_id_list:
            dataset = self.datasets.get_dataset(dataset_id)
            if dataset.project_id != project_id:
                raise ValidationError("Datasets must belong to the same project.")
            datasets.append(dataset)
        _ensure_non_empty(method_hash, "method_hash")
        _ensure_non_empty(code_version, "code_version")
        if status == AnalysisStatus.COMMITTED:
            for dataset in datasets:
                if dataset.status != DatasetStatus.COMMITTED:
                    raise ValidationError(
                        "Analyses can only be created as committed with committed datasets."
                    )
        analysis = Analysis(
            analysis_id=uuid4(),
            project_id=project_id,
            dataset_ids=dataset_id_list,
            method_hash=method_hash.strip(),
            code_version=code_version.strip(),
            environment_hash=environment_hash.strip() if environment_hash else None,
            status=status,
            executed_by=_actor_user_id(actor),
        )
        with self.unit_of_work() as repository:
            repository.analyses.save(analysis)
        return analysis

    def get_analysis(self, analysis_id: UUID) -> Analysis:
        return self.get_from_repository(
            entity_id=analysis_id,
            label="Analysis",
            loader=lambda repository: repository.analyses.get(analysis_id),
        )

    def list_analyses(
        self,
        *,
        project_id: UUID | None = None,
        dataset_id: UUID | None = None,
        question_id: UUID | None = None,
    ) -> list[Analysis]:
        analyses = self.query_from_repository(
            loader=lambda repository: repository.query_analyses(
                project_id=project_id,
                dataset_id=dataset_id,
                question_id=question_id,
                limit=None,
                offset=0,
            ),
        )
        if project_id is not None:
            analyses = [analysis for analysis in analyses if analysis.project_id == project_id]
        if dataset_id is not None:
            analyses = [analysis for analysis in analyses if dataset_id in analysis.dataset_ids]
        if question_id is not None:
            dataset_map = {
                dataset.dataset_id: dataset for dataset in self.datasets.list_datasets()
            }
            analyses = [
                analysis
                for analysis in analyses
                if _analysis_has_question_link(
                    analysis,
                    question_id,
                    dataset_map,
                )
            ]
        return analyses

    def update_analysis(
        self,
        analysis_id: UUID,
        *,
        status: AnalysisStatus | None = None,
        environment_hash: str | None = None,
        actor: AuthContext | None = None,
    ) -> Analysis:
        require_role(actor, WRITE_ROLES)
        analysis = self.get_analysis(analysis_id)
        if analysis.status == AnalysisStatus.COMMITTED:
            if environment_hash is not None:
                raise ValidationError("Committed analyses are immutable.")
            if status == AnalysisStatus.STAGED:
                raise ValidationError("Committed analyses cannot return to staged.")
        if status is not None:
            _ensure_analysis_status_transition(analysis.status, status)
            if status == AnalysisStatus.COMMITTED and analysis.status != AnalysisStatus.COMMITTED:
                self._ensure_analysis_datasets_committed(analysis)
            analysis.status = status
        if environment_hash is not None:
            analysis.environment_hash = environment_hash.strip() if environment_hash else None
        analysis.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.analyses.save(analysis)
        return analysis

    def delete_analysis(self, analysis_id: UUID, *, actor: AuthContext | None = None) -> Analysis:
        require_role(actor, WRITE_ROLES)
        analysis = self.get_analysis(analysis_id)
        with self.unit_of_work() as repository:
            repository.analyses.delete(analysis_id)
        return analysis

    def commit_analysis(
        self,
        analysis_id: UUID,
        *,
        environment_hash: str | None = None,
        claims: Iterable[ClaimInput] | None = None,
        visualizations: Iterable[VisualizationInput] | None = None,
        actor: AuthContext | None = None,
    ) -> tuple[Analysis, list[Claim], list[Visualization]]:
        require_role(actor, WRITE_ROLES)
        analysis = self.get_analysis(analysis_id)
        _ensure_analysis_status_transition(analysis.status, AnalysisStatus.COMMITTED)
        if analysis.status == AnalysisStatus.COMMITTED and environment_hash is not None:
            raise ValidationError("Committed analyses are immutable.")
        if analysis.status != AnalysisStatus.COMMITTED:
            self._ensure_analysis_datasets_committed(analysis)
            analysis.status = AnalysisStatus.COMMITTED
        if environment_hash is not None:
            analysis.environment_hash = environment_hash.strip() if environment_hash else None
        analysis.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.analyses.save(analysis)
        created_claims: list[Claim] = []
        for claim_input in claims or []:
            supported_by_analysis_ids = list(claim_input.supported_by_analysis_ids)
            if analysis.analysis_id not in supported_by_analysis_ids:
                supported_by_analysis_ids.append(analysis.analysis_id)
            created_claims.append(
                self.claims.create_claim(
                    project_id=analysis.project_id,
                    statement=claim_input.statement,
                    confidence=claim_input.confidence,
                    status=claim_input.status,
                    supported_by_dataset_ids=claim_input.supported_by_dataset_ids,
                    supported_by_analysis_ids=supported_by_analysis_ids,
                    actor=actor,
                )
            )
        created_visualizations: list[Visualization] = []
        for viz_input in visualizations or []:
            created_visualizations.append(
                self.visualizations.create_visualization(
                    analysis_id=analysis.analysis_id,
                    viz_type=viz_input.viz_type,
                    file_path=viz_input.file_path,
                    caption=viz_input.caption,
                    related_claim_ids=viz_input.related_claim_ids,
                    actor=actor,
                )
            )
        return analysis, created_claims, created_visualizations

    def _ensure_analysis_datasets_committed(self, analysis: Analysis) -> None:
        for dataset_id in analysis.dataset_ids:
            dataset = self.datasets.get_dataset(dataset_id)
            if dataset.status != DatasetStatus.COMMITTED:
                raise ValidationError("Analyses can only be committed with committed datasets.")
