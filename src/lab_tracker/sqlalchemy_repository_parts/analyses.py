"""Analysis, claim, and visualization SQLAlchemy repositories."""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import (
    AnalysisDatasetModel,
    AnalysisModel,
    ClaimAnalysisModel,
    ClaimDatasetModel,
    ClaimModel,
    VisualizationClaimModel,
    VisualizationModel,
)
from lab_tracker.models import Analysis, Claim, Visualization
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mappers import (
    analysis_dataset_models,
    analysis_from_model,
    analysis_to_model,
    apply_analysis_to_model,
    apply_claim_to_model,
    apply_visualization_to_model,
    claim_analysis_models,
    claim_dataset_models,
    claim_from_model,
    claim_to_model,
    visualization_claim_models,
    visualization_from_model,
    visualization_to_model,
)

from .common import replace_child_rows


class SQLAlchemyAnalysisRepository(EntityRepository[Analysis]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def dataset_map(self, analysis_ids: list[str]) -> dict[str, list[UUID]]:
        dataset_map: dict[str, list[UUID]] = defaultdict(list)
        if not analysis_ids:
            return dataset_map
        rows = self._session.scalars(
            select(AnalysisDatasetModel).where(AnalysisDatasetModel.analysis_id.in_(analysis_ids))
        )
        for row in rows:
            dataset_map[row.analysis_id].append(UUID(row.dataset_id))
        return dataset_map

    def get(self, entity_id: UUID) -> Analysis | None:
        self._session.flush()
        row = self._session.get(AnalysisModel, str(entity_id))
        if row is None:
            return None
        dataset_ids = self.dataset_map([row.analysis_id]).get(row.analysis_id, [])
        return analysis_from_model(row, dataset_ids=dataset_ids)

    def list(self) -> list[Analysis]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(AnalysisModel).order_by(AnalysisModel.created_at, AnalysisModel.analysis_id)
            )
        )
        analysis_ids = [row.analysis_id for row in rows]
        dataset_map = self.dataset_map(analysis_ids)
        return [
            analysis_from_model(row, dataset_ids=dataset_map.get(row.analysis_id, []))
            for row in rows
        ]

    def save(self, entity: Analysis) -> None:
        entity_id = str(entity.analysis_id)
        row = self._session.get(AnalysisModel, entity_id)
        if row is None:
            self._session.add(analysis_to_model(entity))
        else:
            apply_analysis_to_model(row, entity)
        replace_child_rows(
            self._session,
            AnalysisDatasetModel,
            AnalysisDatasetModel.analysis_id,
            entity_id,
            analysis_dataset_models(entity),
        )

    def delete(self, entity_id: UUID) -> Analysis | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(AnalysisModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity


class SQLAlchemyClaimRepository(EntityRepository[Claim]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def dataset_map(self, claim_ids: list[str]) -> dict[str, list[UUID]]:
        dataset_map: dict[str, list[UUID]] = defaultdict(list)
        if not claim_ids:
            return dataset_map
        rows = self._session.scalars(
            select(ClaimDatasetModel).where(ClaimDatasetModel.claim_id.in_(claim_ids))
        )
        for row in rows:
            dataset_map[row.claim_id].append(UUID(row.dataset_id))
        return dataset_map

    def analysis_map(self, claim_ids: list[str]) -> dict[str, list[UUID]]:
        analysis_map: dict[str, list[UUID]] = defaultdict(list)
        if not claim_ids:
            return analysis_map
        rows = self._session.scalars(
            select(ClaimAnalysisModel).where(ClaimAnalysisModel.claim_id.in_(claim_ids))
        )
        for row in rows:
            analysis_map[row.claim_id].append(UUID(row.analysis_id))
        return analysis_map

    def get(self, entity_id: UUID) -> Claim | None:
        self._session.flush()
        row = self._session.get(ClaimModel, str(entity_id))
        if row is None:
            return None
        claim_ids = [row.claim_id]
        dataset_ids = self.dataset_map(claim_ids).get(row.claim_id, [])
        analysis_ids = self.analysis_map(claim_ids).get(row.claim_id, [])
        return claim_from_model(
            row,
            supported_by_dataset_ids=dataset_ids,
            supported_by_analysis_ids=analysis_ids,
        )

    def list(self) -> list[Claim]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(ClaimModel).order_by(ClaimModel.created_at, ClaimModel.claim_id)
            )
        )
        claim_ids = [row.claim_id for row in rows]
        dataset_map = self.dataset_map(claim_ids)
        analysis_map = self.analysis_map(claim_ids)
        return [
            claim_from_model(
                row,
                supported_by_dataset_ids=dataset_map.get(row.claim_id, []),
                supported_by_analysis_ids=analysis_map.get(row.claim_id, []),
            )
            for row in rows
        ]

    def save(self, entity: Claim) -> None:
        entity_id = str(entity.claim_id)
        row = self._session.get(ClaimModel, entity_id)
        if row is None:
            self._session.add(claim_to_model(entity))
        else:
            apply_claim_to_model(row, entity)
        replace_child_rows(
            self._session,
            ClaimDatasetModel,
            ClaimDatasetModel.claim_id,
            entity_id,
            claim_dataset_models(entity),
        )
        replace_child_rows(
            self._session,
            ClaimAnalysisModel,
            ClaimAnalysisModel.claim_id,
            entity_id,
            claim_analysis_models(entity),
        )

    def delete(self, entity_id: UUID) -> Claim | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(ClaimModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity


class SQLAlchemyVisualizationRepository(EntityRepository[Visualization]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def claim_map(self, visualization_ids: list[str]) -> dict[str, list[UUID]]:
        claim_map: dict[str, list[UUID]] = defaultdict(list)
        if not visualization_ids:
            return claim_map
        rows = self._session.scalars(
            select(VisualizationClaimModel).where(
                VisualizationClaimModel.viz_id.in_(visualization_ids)
            )
        )
        for row in rows:
            claim_map[row.viz_id].append(UUID(row.claim_id))
        return claim_map

    def get(self, entity_id: UUID) -> Visualization | None:
        self._session.flush()
        row = self._session.get(VisualizationModel, str(entity_id))
        if row is None:
            return None
        claim_ids = self.claim_map([row.viz_id]).get(row.viz_id, [])
        return visualization_from_model(row, related_claim_ids=claim_ids)

    def list(self) -> list[Visualization]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(VisualizationModel).order_by(
                    VisualizationModel.created_at,
                    VisualizationModel.viz_id,
                )
            )
        )
        visualization_ids = [row.viz_id for row in rows]
        claim_map = self.claim_map(visualization_ids)
        return [
            visualization_from_model(row, related_claim_ids=claim_map.get(row.viz_id, []))
            for row in rows
        ]

    def save(self, entity: Visualization) -> None:
        entity_id = str(entity.viz_id)
        row = self._session.get(VisualizationModel, entity_id)
        if row is None:
            self._session.add(visualization_to_model(entity))
        else:
            apply_visualization_to_model(row, entity)
        replace_child_rows(
            self._session,
            VisualizationClaimModel,
            VisualizationClaimModel.viz_id,
            entity_id,
            visualization_claim_models(entity),
        )

    def delete(self, entity_id: UUID) -> Visualization | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(VisualizationModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity
