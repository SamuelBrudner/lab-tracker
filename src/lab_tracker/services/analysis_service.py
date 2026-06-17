"""Analysis domain service."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    Analysis,
    AnalysisStatus,
    Claim,
    ClaimInput,
    ClaimStatus,
    DatasetStatus,
    EntityOrigin,
    ExternalArtifactReference,
    Visualization,
    VisualizationInput,
    external_artifact_uri_validation_error,
    utc_now,
)
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.dataset_service import DatasetService
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.shared import (
    _analysis_has_question_link,
    _ensure_analysis_status_transition,
    actor_user_fk,
    actor_user_id,
    ensure_non_empty,
    terminal_reason_for_status,
    unique_ids,
)

if TYPE_CHECKING:
    from lab_tracker.services.claim_service import ClaimService
    from lab_tracker.services.visualization_service import VisualizationService


_IMMUTABLE_ANALYSIS_STATUSES = {
    AnalysisStatus.COMMITTED,
    AnalysisStatus.ARCHIVED,
}


class AnalysisService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        datasets: DatasetService,
        claims_provider: Callable[[], ClaimService],
        visualizations_provider: Callable[[], VisualizationService],
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.datasets = datasets
        self._claims_provider = claims_provider
        self._visualizations_provider = visualizations_provider
        self.authorization = authorization

    @property
    def claims(self) -> ClaimService:
        return self._claims_provider()

    @property
    def visualizations(self) -> VisualizationService:
        return self._visualizations_provider()

    def create_analysis(
        self,
        project_id: UUID,
        dataset_ids: Iterable[UUID],
        method_hash: str,
        code_version: str,
        *,
        environment_hash: str | None = None,
        external_artifacts: Iterable[ExternalArtifactReference] | None = None,
        status: AnalysisStatus = AnalysisStatus.STAGED,
        terminal_reason: str | None = None,
        actor: AuthContext | None = None,
        origin: EntityOrigin = EntityOrigin.USER,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Analysis:
        self.authorization.require_contributor(project_id, actor=actor)
        self.projects.get_project(project_id)
        dataset_id_list = unique_ids(dataset_ids)
        if not dataset_id_list:
            raise ValidationError("Analysis must reference at least one dataset.")
        datasets = []
        for dataset_id in dataset_id_list:
            dataset = self.datasets.get_dataset(dataset_id)
            if dataset.project_id != project_id:
                raise ValidationError("Datasets must belong to the same project.")
            datasets.append(dataset)
        ensure_non_empty(method_hash, "method_hash")
        ensure_non_empty(code_version, "code_version")
        if status == AnalysisStatus.COMMITTED:
            for dataset in datasets:
                if dataset.status != DatasetStatus.COMMITTED:
                    raise ValidationError(
                        "Analyses can only be created as committed with committed datasets."
                    )
        resolved_terminal_reason = terminal_reason_for_status(
            None,
            status,
            AnalysisStatus.ARCHIVED,
            terminal_reason,
            entity_name="Analysis",
        )
        analysis = Analysis(
            analysis_id=uuid4(),
            project_id=project_id,
            dataset_ids=dataset_id_list,
            method_hash=method_hash.strip(),
            code_version=code_version.strip(),
            environment_hash=environment_hash.strip() if environment_hash else None,
            external_artifacts=_normalize_external_artifacts(external_artifacts),
            status=status,
            terminal_reason=resolved_terminal_reason,
            executed_by=actor_user_id(actor),
            executed_by_user_id=actor_user_fk(actor, self.repository),
            origin=origin,
            change_set_id=change_set_id,
            origin_provider=origin_provider,
            origin_model=origin_model,
            origin_prompt_version=origin_prompt_version,
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
            dataset_map = {dataset.dataset_id: dataset for dataset in self.datasets.list_datasets()}
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
        external_artifacts: Iterable[ExternalArtifactReference] | None = None,
        terminal_reason: str | None = None,
        actor: AuthContext | None = None,
        origin: EntityOrigin | None = None,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Analysis:
        analysis = self.get_analysis(analysis_id)
        self.authorization.require_contributor(analysis.project_id, actor=actor)
        current_status = analysis.status
        next_status = status or current_status
        if current_status in _IMMUTABLE_ANALYSIS_STATUSES:
            if environment_hash is not None:
                raise ValidationError("Committed analyses are immutable.")
            if external_artifacts is not None:
                raise ValidationError("Committed analyses are immutable.")
        if current_status == AnalysisStatus.COMMITTED and status == AnalysisStatus.STAGED:
            raise ValidationError("Committed analyses cannot return to staged.")
        if status is not None:
            _ensure_analysis_status_transition(current_status, status)
            if status == AnalysisStatus.COMMITTED and current_status != AnalysisStatus.COMMITTED:
                self._ensure_analysis_datasets_committed(analysis)
        resolved_terminal_reason = terminal_reason_for_status(
            current_status,
            next_status,
            AnalysisStatus.ARCHIVED,
            terminal_reason,
            entity_name="Analysis",
        )
        if status is not None:
            analysis.status = status
        if resolved_terminal_reason is not None:
            analysis.terminal_reason = resolved_terminal_reason
        if environment_hash is not None:
            analysis.environment_hash = environment_hash.strip() if environment_hash else None
        if external_artifacts is not None:
            analysis.external_artifacts = _normalize_external_artifacts(external_artifacts)
        if origin is not None:
            analysis.origin = origin
        if change_set_id is not None:
            analysis.change_set_id = change_set_id
        if origin_provider is not None:
            analysis.origin_provider = origin_provider
        if origin_model is not None:
            analysis.origin_model = origin_model
        if origin_prompt_version is not None:
            analysis.origin_prompt_version = origin_prompt_version
        analysis.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.analyses.save(analysis)
        return analysis

    def delete_analysis(self, analysis_id: UUID, *, actor: AuthContext | None = None) -> Analysis:
        analysis = self.get_analysis(analysis_id)
        self.authorization.require_contributor(analysis.project_id, actor=actor)
        self._ensure_analysis_can_be_deleted(analysis)
        with self.unit_of_work() as repository:
            repository.analyses.delete(analysis_id)
        return analysis

    def commit_analysis(
        self,
        analysis_id: UUID,
        *,
        environment_hash: str | None = None,
        external_artifacts: Iterable[ExternalArtifactReference] | None = None,
        claims: Iterable[ClaimInput] | None = None,
        visualizations: Iterable[VisualizationInput] | None = None,
        actor: AuthContext | None = None,
    ) -> tuple[Analysis, list[Claim], list[Visualization]]:
        analysis = self.get_analysis(analysis_id)
        self.authorization.require_contributor(analysis.project_id, actor=actor)
        _ensure_analysis_status_transition(analysis.status, AnalysisStatus.COMMITTED)
        if analysis.status == AnalysisStatus.COMMITTED and (
            environment_hash is not None or external_artifacts is not None
        ):
            raise ValidationError("Committed analyses are immutable.")
        if analysis.status != AnalysisStatus.COMMITTED:
            self._ensure_analysis_datasets_committed(analysis)
            analysis.status = AnalysisStatus.COMMITTED
        if environment_hash is not None:
            analysis.environment_hash = environment_hash.strip() if environment_hash else None
        if external_artifacts is not None:
            analysis.external_artifacts = _normalize_external_artifacts(external_artifacts)
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
                    terminal_reason=claim_input.terminal_reason,
                    supported_by_dataset_ids=claim_input.supported_by_dataset_ids,
                    supported_by_analysis_ids=supported_by_analysis_ids,
                    answers_question_ids=claim_input.answers_question_ids,
                    external_citations=claim_input.external_citations,
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

    def _ensure_analysis_can_be_deleted(self, analysis: Analysis) -> None:
        claims = self.query_from_repository(
            loader=lambda repository: repository.query_claims(
                analysis_id=analysis.analysis_id,
                limit=None,
                offset=0,
            ),
        )
        for claim in claims:
            if claim.status in {ClaimStatus.PROPOSED, ClaimStatus.REJECTED}:
                continue
            remaining_analysis_ids = [
                analysis_id
                for analysis_id in claim.supported_by_analysis_ids
                if analysis_id != analysis.analysis_id
            ]
            if not claim.supported_by_dataset_ids and not remaining_analysis_ids:
                raise ValidationError(
                    "Analysis cannot be deleted while it is the last support link "
                    "for a non-proposed claim."
                )


def _normalize_external_artifacts(
    artifacts: Iterable[ExternalArtifactReference] | None,
) -> list[ExternalArtifactReference]:
    normalized: list[ExternalArtifactReference] = []
    seen: set[tuple[str, str, str]] = set()
    for artifact in artifacts or []:
        item = ExternalArtifactReference.model_validate(artifact)
        reason = external_artifact_uri_validation_error(item.uri)
        if reason is not None:
            raise ValidationError(reason)
        key = (item.kind.value, item.source_system, item.uri)
        if key in seen:
            raise ValidationError("Duplicate external artifact reference on analysis.")
        seen.add(key)
        normalized.append(item)
    return normalized
