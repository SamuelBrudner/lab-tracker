"""Visualization domain service."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ValidationError
from lab_tracker.models import EntityOrigin, EntityType, Visualization, utc_now
from lab_tracker.patching import NOT_PROVIDED, PatchValue, is_provided
from lab_tracker.services.analysis_service import AnalysisService
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.claim_service import ClaimService
from lab_tracker.services.goal_link_cleanup import remove_goal_links_to_entity
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.shared import (
    actor_user_fk,
    actor_user_id,
    ensure_non_empty,
    unique_ids,
)


class VisualizationService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        analyses: AnalysisService,
        claims: ClaimService,
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.analyses = analyses
        self.claims = claims
        self.authorization = authorization

    def create_visualization(
        self,
        analysis_id: UUID,
        viz_type: str,
        file_path: str,
        *,
        caption: str | None = None,
        related_claim_ids: Iterable[UUID] | None = None,
        actor: AuthContext | None = None,
        origin: EntityOrigin = EntityOrigin.USER,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Visualization:
        analysis = self.analyses.get_analysis(analysis_id)
        self.authorization.require_contributor(analysis.project_id, actor=actor)
        ensure_non_empty(viz_type, "viz_type")
        ensure_non_empty(file_path, "file_path")
        claim_ids = unique_ids(related_claim_ids)
        for claim_id in claim_ids:
            claim = self.claims.get_claim(claim_id)
            if claim.project_id != analysis.project_id:
                raise ValidationError("Related claims must belong to the same project.")
        visualization = Visualization(
            viz_id=uuid4(),
            analysis_id=analysis_id,
            dataset_ids=list(analysis.dataset_ids),
            viz_type=viz_type.strip(),
            file_path=file_path.strip(),
            caption=caption.strip() if caption else None,
            related_claim_ids=claim_ids,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
            origin=origin,
            change_set_id=change_set_id,
            origin_provider=origin_provider,
            origin_model=origin_model,
            origin_prompt_version=origin_prompt_version,
        )
        with self.unit_of_work() as repository:
            repository.visualizations.save(visualization)
        return visualization

    def get_visualization(self, viz_id: UUID) -> Visualization:
        return self.get_from_repository(
            entity_id=viz_id,
            label="Visualization",
            loader=lambda repository: repository.visualizations.get(viz_id),
        )

    def list_visualizations(
        self,
        *,
        project_id: UUID | None = None,
        analysis_id: UUID | None = None,
        claim_id: UUID | None = None,
    ) -> list[Visualization]:
        return self.query_from_repository(
            loader=lambda repository: repository.query_visualizations(
                project_id=project_id,
                analysis_id=analysis_id,
                claim_id=claim_id,
                limit=None,
                offset=0,
            ),
        )

    def update_visualization(
        self,
        viz_id: UUID,
        *,
        viz_type: PatchValue[str | None] = NOT_PROVIDED,
        file_path: PatchValue[str | None] = NOT_PROVIDED,
        caption: PatchValue[str | None] = NOT_PROVIDED,
        related_claim_ids: PatchValue[Iterable[UUID] | None] = NOT_PROVIDED,
        actor: AuthContext | None = None,
        origin: EntityOrigin | None = None,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Visualization:
        visualization = self.get_visualization(viz_id)
        analysis = self.analyses.get_analysis(visualization.analysis_id)
        self.authorization.require_contributor(analysis.project_id, actor=actor)
        before = visualization.model_copy(deep=True)
        if is_provided(viz_type):
            if viz_type is None:
                raise ValidationError("viz_type must not be null.")
            ensure_non_empty(viz_type, "viz_type")
            visualization.viz_type = viz_type.strip()
        if is_provided(file_path):
            if file_path is None:
                raise ValidationError("file_path must not be null.")
            ensure_non_empty(file_path, "file_path")
            visualization.file_path = file_path.strip()
        if is_provided(caption):
            visualization.caption = caption.strip() if caption else None
        if is_provided(related_claim_ids):
            if related_claim_ids is None:
                raise ValidationError("related_claim_ids must not be null.")
            claim_ids = unique_ids(related_claim_ids)
            for claim_id in claim_ids:
                claim = self.claims.get_claim(claim_id)
                if claim.project_id != analysis.project_id:
                    raise ValidationError("Related claims must belong to the same project.")
            visualization.related_claim_ids = claim_ids
        if origin is not None:
            visualization.origin = origin
        if change_set_id is not None:
            visualization.change_set_id = change_set_id
        if origin_provider is not None:
            visualization.origin_provider = origin_provider
        if origin_model is not None:
            visualization.origin_model = origin_model
        if origin_prompt_version is not None:
            visualization.origin_prompt_version = origin_prompt_version
        if visualization == before:
            return visualization
        visualization.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.visualizations.save(visualization)
        return visualization

    def delete_visualization(
        self,
        viz_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Visualization:
        visualization = self.get_visualization(viz_id)
        analysis = self.analyses.get_analysis(visualization.analysis_id)
        self.authorization.require_contributor(analysis.project_id, actor=actor)
        with self.unit_of_work() as repository:
            remove_goal_links_to_entity(
                repository,
                entity_type=EntityType.VISUALIZATION,
                entity_id=viz_id,
            )
            repository.visualizations.delete(viz_id)
        return visualization
