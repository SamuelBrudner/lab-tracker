"""Claim domain service."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    Claim,
    ClaimStatus,
    utc_now,
)
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.dataset_service import DatasetService
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.shared import (
    WRITE_ROLES,
    _ensure_claim_confidence,
    _ensure_claim_status_transition,
    _ensure_claim_support_links,
    ensure_non_empty,
    unique_ids,
)

if TYPE_CHECKING:
    from lab_tracker.services.analysis_service import AnalysisService


class ClaimService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        datasets: DatasetService,
        analyses_provider: Callable[[], AnalysisService],
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.datasets = datasets
        self._analyses_provider = analyses_provider

    @property
    def analyses(self) -> AnalysisService:
        return self._analyses_provider()

    def create_claim(
        self,
        project_id: UUID,
        statement: str,
        confidence: float,
        *,
        status: ClaimStatus = ClaimStatus.PROPOSED,
        supported_by_dataset_ids: Iterable[UUID] | None = None,
        supported_by_analysis_ids: Iterable[UUID] | None = None,
        actor: AuthContext | None = None,
    ) -> Claim:
        require_role(actor, WRITE_ROLES)
        self.projects.get_project(project_id)
        ensure_non_empty(statement, "statement")
        _ensure_claim_confidence(confidence)
        dataset_ids, analysis_ids = self._resolve_claim_support_links(
            project_id,
            supported_by_dataset_ids,
            supported_by_analysis_ids,
        )
        _ensure_claim_support_links(status, dataset_ids, analysis_ids)
        claim = Claim(
            claim_id=uuid4(),
            project_id=project_id,
            statement=statement.strip(),
            confidence=confidence,
            status=status,
            supported_by_dataset_ids=dataset_ids,
            supported_by_analysis_ids=analysis_ids,
        )
        with self.unit_of_work() as repository:
            repository.claims.save(claim)
        return claim

    def get_claim(self, claim_id: UUID) -> Claim:
        return self.get_from_repository(
            entity_id=claim_id,
            label="Claim",
            loader=lambda repository: repository.claims.get(claim_id),
        )

    def list_claims(
        self,
        *,
        project_id: UUID | None = None,
        status: ClaimStatus | None = None,
        dataset_id: UUID | None = None,
        analysis_id: UUID | None = None,
    ) -> list[Claim]:
        return self.query_from_repository(
            loader=lambda repository: repository.query_claims(
                project_id=project_id,
                status=status.value if status is not None else None,
                dataset_id=dataset_id,
                analysis_id=analysis_id,
                limit=None,
                offset=0,
            ),
        )

    def update_claim(
        self,
        claim_id: UUID,
        *,
        statement: str | None = None,
        confidence: float | None = None,
        status: ClaimStatus | None = None,
        supported_by_dataset_ids: Iterable[UUID] | None = None,
        supported_by_analysis_ids: Iterable[UUID] | None = None,
        actor: AuthContext | None = None,
    ) -> Claim:
        require_role(actor, WRITE_ROLES)
        claim = self.get_claim(claim_id)
        next_status = status or claim.status
        _ensure_claim_status_transition(claim.status, next_status)
        if claim.status != ClaimStatus.PROPOSED and (
            statement is not None
            or confidence is not None
            or supported_by_dataset_ids is not None
            or supported_by_analysis_ids is not None
        ):
            raise ValidationError("Only proposed claims can be edited.")
        if statement is not None:
            ensure_non_empty(statement, "statement")
            claim.statement = statement.strip()
        if confidence is not None:
            _ensure_claim_confidence(confidence)
            claim.confidence = confidence
        if supported_by_dataset_ids is not None or supported_by_analysis_ids is not None:
            dataset_ids, analysis_ids = self._resolve_claim_support_links(
                claim.project_id,
                supported_by_dataset_ids or claim.supported_by_dataset_ids,
                supported_by_analysis_ids or claim.supported_by_analysis_ids,
            )
            claim.supported_by_dataset_ids = dataset_ids
            claim.supported_by_analysis_ids = analysis_ids
        _ensure_claim_support_links(
            next_status, claim.supported_by_dataset_ids, claim.supported_by_analysis_ids
        )
        if status is not None:
            claim.status = status
        claim.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.claims.save(claim)
        return claim

    def delete_claim(self, claim_id: UUID, *, actor: AuthContext | None = None) -> Claim:
        require_role(actor, WRITE_ROLES)
        claim = self.get_claim(claim_id)
        with self.unit_of_work() as repository:
            repository.claims.delete(claim_id)
        return claim

    def _resolve_claim_support_links(
        self,
        project_id: UUID,
        dataset_ids: Iterable[UUID] | None,
        analysis_ids: Iterable[UUID] | None,
    ) -> tuple[list[UUID], list[UUID]]:
        resolved_dataset_ids = unique_ids(dataset_ids)
        resolved_analysis_ids = unique_ids(analysis_ids)
        for dataset_id in resolved_dataset_ids:
            dataset = self.datasets.get_dataset(dataset_id)
            if dataset.project_id != project_id:
                raise ValidationError("Supporting datasets must belong to the same project.")
        for analysis_id in resolved_analysis_ids:
            analysis = self.analyses.get_analysis(analysis_id)
            if analysis.project_id != project_id:
                raise ValidationError("Supporting analyses must belong to the same project.")
        return resolved_dataset_ids, resolved_analysis_ids
