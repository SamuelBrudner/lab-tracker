"""Claim domain service."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    Claim,
    ClaimEdge,
    ClaimRelation,
    ClaimStatus,
    EntityOrigin,
    EntityType,
    ExternalArtifactReference,
    external_artifact_uri_validation_error,
    utc_now,
)
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.dataset_service import DatasetService
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.question_service import QuestionService
from lab_tracker.services.shared import (
    _ensure_claim_confidence,
    _ensure_claim_status_transition,
    _ensure_claim_support_links,
    actor_user_fk,
    actor_user_id,
    ensure_non_empty,
    terminal_reason_for_status,
    unique_ids,
)

if TYPE_CHECKING:
    from lab_tracker.services.analysis_service import AnalysisService
    from lab_tracker.services.entity_version_service import EntityVersionService


class ClaimService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        datasets: DatasetService,
        questions: QuestionService,
        analyses_provider: Callable[[], AnalysisService],
        versions: EntityVersionService,
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.datasets = datasets
        self.questions = questions
        self._analyses_provider = analyses_provider
        self.versions = versions
        self.authorization = authorization

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
        terminal_reason: str | None = None,
        supported_by_dataset_ids: Iterable[UUID] | None = None,
        supported_by_analysis_ids: Iterable[UUID] | None = None,
        answers_question_ids: Iterable[UUID] | None = None,
        external_citations: Iterable[ExternalArtifactReference] | None = None,
        actor: AuthContext | None = None,
        origin: EntityOrigin = EntityOrigin.USER,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Claim:
        self.authorization.require_contributor(project_id, actor=actor)
        self.projects.get_project(project_id)
        ensure_non_empty(statement, "statement")
        _ensure_claim_confidence(confidence)
        dataset_ids, analysis_ids = self._resolve_claim_support_links(
            project_id,
            supported_by_dataset_ids,
            supported_by_analysis_ids,
        )
        question_ids = self._resolve_claim_question_links(project_id, answers_question_ids)
        citations = _normalize_external_citations(external_citations)
        _ensure_claim_support_links(status, dataset_ids, analysis_ids)
        resolved_terminal_reason = terminal_reason_for_status(
            None,
            status,
            ClaimStatus.REJECTED,
            terminal_reason,
            entity_name="Claim",
        )
        claim = Claim(
            claim_id=uuid4(),
            project_id=project_id,
            statement=statement.strip(),
            confidence=confidence,
            status=status,
            terminal_reason=resolved_terminal_reason,
            supported_by_dataset_ids=dataset_ids,
            supported_by_analysis_ids=analysis_ids,
            answers_question_ids=question_ids,
            external_citations=citations,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
            origin=origin,
            change_set_id=change_set_id,
            origin_provider=origin_provider,
            origin_model=origin_model,
            origin_prompt_version=origin_prompt_version,
        )
        with self.unit_of_work() as repository:
            repository.claims.save(claim)
            self.versions.record_entity_version(
                repository,
                entity_type=EntityType.CLAIM,
                entity_id=claim.claim_id,
                entity=claim,
                actor=actor,
            )
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
        terminal_reason: str | None = None,
        supported_by_dataset_ids: Iterable[UUID] | None = None,
        supported_by_analysis_ids: Iterable[UUID] | None = None,
        answers_question_ids: Iterable[UUID] | None = None,
        external_citations: Iterable[ExternalArtifactReference] | None = None,
        actor: AuthContext | None = None,
        origin: EntityOrigin | None = None,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Claim:
        claim = self.get_claim(claim_id)
        self.authorization.require_contributor(claim.project_id, actor=actor)
        next_status = status or claim.status
        _ensure_claim_status_transition(claim.status, next_status)
        resolved_terminal_reason = terminal_reason_for_status(
            claim.status,
            next_status,
            ClaimStatus.REJECTED,
            terminal_reason,
            entity_name="Claim",
        )
        if claim.status != ClaimStatus.PROPOSED and (
            statement is not None
            or confidence is not None
            or supported_by_dataset_ids is not None
            or supported_by_analysis_ids is not None
            or answers_question_ids is not None
            or external_citations is not None
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
        if answers_question_ids is not None:
            claim.answers_question_ids = self._resolve_claim_question_links(
                claim.project_id, answers_question_ids
            )
        if external_citations is not None:
            claim.external_citations = _normalize_external_citations(external_citations)
        _ensure_claim_support_links(
            next_status, claim.supported_by_dataset_ids, claim.supported_by_analysis_ids
        )
        if status is not None:
            claim.status = status
        if resolved_terminal_reason is not None:
            claim.terminal_reason = resolved_terminal_reason
        if origin is not None:
            claim.origin = origin
        if change_set_id is not None:
            claim.change_set_id = change_set_id
        if origin_provider is not None:
            claim.origin_provider = origin_provider
        if origin_model is not None:
            claim.origin_model = origin_model
        if origin_prompt_version is not None:
            claim.origin_prompt_version = origin_prompt_version
        claim.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.claims.save(claim)
            self.versions.record_entity_version(
                repository,
                entity_type=EntityType.CLAIM,
                entity_id=claim.claim_id,
                entity=claim,
                actor=actor,
            )
        return claim

    def delete_claim(self, claim_id: UUID, *, actor: AuthContext | None = None) -> Claim:
        claim = self.get_claim(claim_id)
        self.authorization.require_contributor(claim.project_id, actor=actor)
        with self.unit_of_work() as repository:
            repository.claims.delete(claim_id)
        return claim

    def create_claim_edge(
        self,
        claim_id: UUID,
        *,
        target_claim_id: UUID,
        relation: ClaimRelation,
        actor: AuthContext | None = None,
    ) -> ClaimEdge:
        claim = self.get_claim(claim_id)
        target_claim = self.get_claim(target_claim_id)
        self.authorization.require_contributor(claim.project_id, actor=actor)
        if claim.project_id != target_claim.project_id:
            raise ValidationError("Claim edges must link claims in the same project.")
        if claim.claim_id == target_claim.claim_id:
            raise ValidationError("Claim edges cannot target the source claim.")
        existing_edges = self.list_claim_edges(project_id=claim.project_id)
        if any(
            edge.claim_id == claim.claim_id
            and edge.target_claim_id == target_claim.claim_id
            and edge.relation == relation
            for edge in existing_edges
        ):
            raise ValidationError("Duplicate claim edge.")
        _ensure_claim_edge_would_not_cycle(
            existing_edges,
            claim_id=claim.claim_id,
            target_claim_id=target_claim.claim_id,
        )
        edge = ClaimEdge(
            edge_id=uuid4(),
            claim_id=claim.claim_id,
            target_claim_id=target_claim.claim_id,
            relation=relation,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
        )
        with self.unit_of_work() as repository:
            repository.claim_edges.save(edge)
        return edge

    def list_claim_edges(
        self,
        *,
        project_id: UUID | None = None,
        claim_id: UUID | None = None,
        target_claim_id: UUID | None = None,
        relation: ClaimRelation | None = None,
    ) -> list[ClaimEdge]:
        if project_id is not None:
            self.projects.get_project(project_id)
        return self.query_from_repository(
            loader=lambda repository: repository.query_claim_edges(
                project_id=project_id,
                claim_id=claim_id,
                target_claim_id=target_claim_id,
                relation=relation.value if relation is not None else None,
                limit=None,
                offset=0,
            ),
        )

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

    def _resolve_claim_question_links(
        self,
        project_id: UUID,
        question_ids: Iterable[UUID] | None,
    ) -> list[UUID]:
        resolved_question_ids = unique_ids(question_ids)
        for question_id in resolved_question_ids:
            question = self.questions.get_question(question_id)
            if question.project_id != project_id:
                raise ValidationError("Answered questions must belong to the same project.")
        return resolved_question_ids


def _normalize_external_citations(
    citations: Iterable[ExternalArtifactReference] | None,
) -> list[ExternalArtifactReference]:
    normalized: list[ExternalArtifactReference] = []
    seen: set[tuple[str, str, str]] = set()
    for citation in citations or []:
        item = ExternalArtifactReference.model_validate(citation)
        reason = external_artifact_uri_validation_error(item.uri)
        if reason is not None:
            raise ValidationError(reason)
        key = (item.kind.value, item.source_system, item.uri)
        if key in seen:
            raise ValidationError("Duplicate external citation reference on claim.")
        seen.add(key)
        normalized.append(item)
    return normalized


def _ensure_claim_edge_would_not_cycle(
    edges: Iterable[ClaimEdge],
    *,
    claim_id: UUID,
    target_claim_id: UUID,
) -> None:
    adjacency: dict[UUID, set[UUID]] = {}
    for edge in edges:
        adjacency.setdefault(edge.claim_id, set()).add(edge.target_claim_id)
    stack = [target_claim_id]
    seen: set[UUID] = set()
    while stack:
        current = stack.pop()
        if current == claim_id:
            raise ValidationError("Claim edge would create a cycle.")
        if current in seen:
            continue
        seen.add(current)
        stack.extend(adjacency.get(current, set()))
