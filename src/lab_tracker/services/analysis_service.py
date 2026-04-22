"""Analysis domain service mixin."""

from __future__ import annotations

from typing import Iterable
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


class AnalysisServiceMixin:
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
        self.get_project(project_id)
        dataset_id_list = _unique_ids(dataset_ids)
        if not dataset_id_list:
            raise ValidationError("Analysis must reference at least one dataset.")
        datasets = []
        for dataset_id in dataset_id_list:
            dataset = self.get_dataset(dataset_id)
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
        self._store.analyses[analysis.analysis_id] = analysis
        self._run_repository_write(lambda repository: repository.analyses.save(analysis))
        return analysis

    def get_analysis(self, analysis_id: UUID) -> Analysis:
        return self._get_from_repository_or_store(
            attribute_name="analyses",
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
        repository = self._active_repository()
        if repository is not None and not self._allow_in_memory:
            analyses, _ = repository.query_analyses(
                project_id=project_id,
                dataset_id=dataset_id,
                question_id=question_id,
                limit=None,
                offset=0,
            )
            return self._cache_entities(
                "analyses",
                analyses,
                lambda analysis: analysis.analysis_id,
            )
        else:
            if project_id is None:
                analyses = list(self._store.analyses.values())
            else:
                analyses = [a for a in self._store.analyses.values() if a.project_id == project_id]
        if project_id is not None:
            analyses = [analysis for analysis in analyses if analysis.project_id == project_id]
        if dataset_id is not None:
            analyses = [analysis for analysis in analyses if dataset_id in analysis.dataset_ids]
        if question_id is not None:
            dataset_map = {dataset.dataset_id: dataset for dataset in self.list_datasets()}
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
        self._run_repository_write(lambda repository: repository.analyses.save(analysis))
        return analysis

    def delete_analysis(self, analysis_id: UUID, *, actor: AuthContext | None = None) -> Analysis:
        require_role(actor, WRITE_ROLES)
        analysis = self.get_analysis(analysis_id)
        self._store.analyses.pop(analysis_id, None)
        self._run_repository_write(lambda repository: repository.analyses.delete(analysis_id))
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
        self._run_repository_write(lambda repository: repository.analyses.save(analysis))
        created_claims: list[Claim] = []
        for claim_input in claims or []:
            supported_by_analysis_ids = list(claim_input.supported_by_analysis_ids)
            if analysis.analysis_id not in supported_by_analysis_ids:
                supported_by_analysis_ids.append(analysis.analysis_id)
            created_claims.append(
                self.create_claim(
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
                self.create_visualization(
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
            dataset = self.get_dataset(dataset_id)
            if dataset.status != DatasetStatus.COMMITTED:
                raise ValidationError("Analyses can only be committed with committed datasets.")
