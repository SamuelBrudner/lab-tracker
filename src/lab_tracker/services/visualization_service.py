"""Visualization domain service mixin."""

from __future__ import annotations

from typing import Iterable
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.errors import ValidationError
from lab_tracker.models import Visualization, utc_now
from lab_tracker.services.shared import (
    WRITE_ROLES,
    _ensure_non_empty,
    _unique_ids,
)


class VisualizationServiceMixin:
    def create_visualization(
        self,
        analysis_id: UUID,
        viz_type: str,
        file_path: str,
        *,
        caption: str | None = None,
        related_claim_ids: Iterable[UUID] | None = None,
        actor: AuthContext | None = None,
    ) -> Visualization:
        require_role(actor, WRITE_ROLES)
        analysis = self.get_analysis(analysis_id)
        _ensure_non_empty(viz_type, "viz_type")
        _ensure_non_empty(file_path, "file_path")
        claim_ids = _unique_ids(related_claim_ids)
        for claim_id in claim_ids:
            claim = self.get_claim(claim_id)
            if claim.project_id != analysis.project_id:
                raise ValidationError("Related claims must belong to the same project.")
        visualization = Visualization(
            viz_id=uuid4(),
            analysis_id=analysis_id,
            viz_type=viz_type.strip(),
            file_path=file_path.strip(),
            caption=caption.strip() if caption else None,
            related_claim_ids=claim_ids,
        )
        self._store.visualizations[visualization.viz_id] = visualization
        self._run_repository_write(lambda repository: repository.visualizations.save(visualization))
        return visualization

    def get_visualization(self, viz_id: UUID) -> Visualization:
        return self._get_from_repository_or_store(
            attribute_name="visualizations",
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
        repository = self._active_repository()
        if repository is not None and not self._allow_in_memory:
            query_repo = getattr(repository, "query_visualizations", None)
            if query_repo is not None:
                visualizations, _ = query_repo(
                    project_id=project_id,
                    analysis_id=analysis_id,
                    claim_id=claim_id,
                    limit=None,
                    offset=0,
                )
                return self._cache_entities(
                    "visualizations",
                    visualizations,
                    lambda visualization: visualization.viz_id,
                )
            visualizations = self._list_from_repository_or_store(
                attribute_name="visualizations",
                loader=lambda current_repository: current_repository.visualizations.list(),
                entity_id_getter=lambda visualization: visualization.viz_id,
            )
            if project_id is not None:
                visualizations = [
                    visualization
                    for visualization in visualizations
                    if self.get_analysis(visualization.analysis_id).project_id == project_id
                ]
            if analysis_id is not None:
                visualizations = [
                    visualization
                    for visualization in visualizations
                    if visualization.analysis_id == analysis_id
                ]
            if claim_id is not None:
                visualizations = [
                    visualization
                    for visualization in visualizations
                    if claim_id in visualization.related_claim_ids
                ]
            return visualizations
        if project_id is None:
            visualizations = list(self._store.visualizations.values())
        else:
            visualizations = [
                viz
                for viz in self._store.visualizations.values()
                if self.get_analysis(viz.analysis_id).project_id == project_id
            ]
        if analysis_id is not None:
            visualizations = [viz for viz in visualizations if viz.analysis_id == analysis_id]
        if claim_id is not None:
            visualizations = [viz for viz in visualizations if claim_id in viz.related_claim_ids]
        return visualizations

    def update_visualization(
        self,
        viz_id: UUID,
        *,
        viz_type: str | None = None,
        file_path: str | None = None,
        caption: str | None = None,
        related_claim_ids: Iterable[UUID] | None = None,
        actor: AuthContext | None = None,
    ) -> Visualization:
        require_role(actor, WRITE_ROLES)
        visualization = self.get_visualization(viz_id)
        if viz_type is not None:
            _ensure_non_empty(viz_type, "viz_type")
            visualization.viz_type = viz_type.strip()
        if file_path is not None:
            _ensure_non_empty(file_path, "file_path")
            visualization.file_path = file_path.strip()
        if caption is not None:
            visualization.caption = caption.strip() if caption else None
        if related_claim_ids is not None:
            claim_ids = _unique_ids(related_claim_ids)
            analysis = self.get_analysis(visualization.analysis_id)
            for claim_id in claim_ids:
                claim = self.get_claim(claim_id)
                if claim.project_id != analysis.project_id:
                    raise ValidationError("Related claims must belong to the same project.")
            visualization.related_claim_ids = claim_ids
        visualization.updated_at = utc_now()
        self._run_repository_write(lambda repository: repository.visualizations.save(visualization))
        return visualization

    def delete_visualization(
        self,
        viz_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Visualization:
        require_role(actor, WRITE_ROLES)
        visualization = self.get_visualization(viz_id)
        self._store.visualizations.pop(viz_id, None)
        self._run_repository_write(lambda repository: repository.visualizations.delete(viz_id))
        return visualization
