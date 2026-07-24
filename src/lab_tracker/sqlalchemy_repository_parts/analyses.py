"""Analysis, claim, and visualization SQLAlchemy repositories."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import (
    AnalysisDatasetModel,
    AnalysisModel,
    ClaimAnalysisModel,
    ClaimDatasetModel,
    ClaimEdgeModel,
    ClaimModel,
    ClaimQuestionModel,
    DatasetModel,
    DatasetQuestionLinkModel,
    VisualizationClaimModel,
    VisualizationModel,
)
from lab_tracker.db_types import ensure_uuid
from lab_tracker.models import Analysis, Claim, ClaimEdge, Visualization
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mappers import (
    analysis_dataset_models,
    analysis_from_model,
    analysis_to_model,
    apply_analysis_to_model,
    apply_claim_edge_to_model,
    apply_claim_to_model,
    apply_visualization_to_model,
    claim_analysis_models,
    claim_dataset_models,
    claim_edge_from_model,
    claim_edge_to_model,
    claim_from_model,
    claim_question_models,
    claim_to_model,
    visualization_claim_models,
    visualization_from_model,
    visualization_to_model,
)

from .common import apply_pagination, count_from_statement, replace_child_rows, uuid_values


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
            dataset_map[str(row.analysis_id)].append(ensure_uuid(row.dataset_id))
        return dataset_map

    def analyses_from_rows(self, rows: list[AnalysisModel]) -> list[Analysis]:
        analysis_ids = [row.analysis_id for row in rows]
        dataset_map = self.dataset_map(analysis_ids)
        return [
            analysis_from_model(row, dataset_ids=dataset_map.get(str(row.analysis_id), []))
            for row in rows
        ]

    def get(self, entity_id: UUID) -> Analysis | None:
        self._session.flush()
        row = self._session.get(AnalysisModel, str(entity_id))
        if row is None:
            return None
        return self.analyses_from_rows([row])[0]

    def list(self) -> list[Analysis]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(AnalysisModel).order_by(AnalysisModel.created_at, AnalysisModel.analysis_id)
            )
        )
        return self.analyses_from_rows(rows)

    def save(self, entity: Analysis) -> None:
        entity_id = str(entity.analysis_id)
        row = self._session.get(AnalysisModel, entity_id)
        if row is None:
            self._session.add(analysis_to_model(entity))
        else:
            apply_analysis_to_model(row, entity)
        self._session.flush()
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

    def query(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        dataset_id: UUID | None = None,
        question_id: UUID | None = None,
        status: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Analysis], int]:
        self._session.flush()
        if project_ids is not None and not project_ids:
            return [], 0
        stmt = select(AnalysisModel)
        count_stmt = select(AnalysisModel.analysis_id)
        distinct_required = False
        joined_analysis_datasets = False
        if project_id is not None:
            stmt = stmt.where(AnalysisModel.project_id == str(project_id))
            count_stmt = count_stmt.where(AnalysisModel.project_id == str(project_id))
        if project_ids is not None:
            project_values = uuid_values(project_ids)
            stmt = stmt.where(AnalysisModel.project_id.in_(project_values))
            count_stmt = count_stmt.where(AnalysisModel.project_id.in_(project_values))
        if dataset_id is not None:
            stmt = stmt.join(
                AnalysisDatasetModel,
                AnalysisDatasetModel.analysis_id == AnalysisModel.analysis_id,
            ).where(AnalysisDatasetModel.dataset_id == str(dataset_id))
            count_stmt = count_stmt.join(
                AnalysisDatasetModel,
                AnalysisDatasetModel.analysis_id == AnalysisModel.analysis_id,
            ).where(AnalysisDatasetModel.dataset_id == str(dataset_id))
            joined_analysis_datasets = True
        if question_id is not None:
            distinct_required = True
            if not joined_analysis_datasets:
                stmt = stmt.join(
                    AnalysisDatasetModel,
                    AnalysisDatasetModel.analysis_id == AnalysisModel.analysis_id,
                )
                count_stmt = count_stmt.join(
                    AnalysisDatasetModel,
                    AnalysisDatasetModel.analysis_id == AnalysisModel.analysis_id,
                )
            stmt = stmt.join(
                DatasetQuestionLinkModel,
                DatasetQuestionLinkModel.dataset_id == AnalysisDatasetModel.dataset_id,
            ).where(DatasetQuestionLinkModel.question_id == str(question_id))
            count_stmt = count_stmt.join(
                DatasetQuestionLinkModel,
                DatasetQuestionLinkModel.dataset_id == AnalysisDatasetModel.dataset_id,
            ).where(DatasetQuestionLinkModel.question_id == str(question_id))
        if status is not None:
            stmt = stmt.where(AnalysisModel.status == status)
            count_stmt = count_stmt.where(AnalysisModel.status == status)
        if since is not None:
            stmt = stmt.where(AnalysisModel.created_at >= since)
            count_stmt = count_stmt.where(AnalysisModel.created_at >= since)
        if until is not None:
            stmt = stmt.where(AnalysisModel.created_at < until)
            count_stmt = count_stmt.where(AnalysisModel.created_at < until)
        if created_by is not None:
            stmt = stmt.where(AnalysisModel.executed_by_user_id == created_by)
            count_stmt = count_stmt.where(AnalysisModel.executed_by_user_id == created_by)
        if distinct_required:
            stmt = stmt.distinct()
            count_stmt = count_stmt.distinct()
        if recent_first:
            stmt = stmt.order_by(
                AnalysisModel.created_at.desc(),
                AnalysisModel.analysis_id.desc(),
            )
        else:
            stmt = stmt.order_by(AnalysisModel.created_at, AnalysisModel.analysis_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self.analyses_from_rows(rows), total


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
            dataset_map[str(row.claim_id)].append(ensure_uuid(row.dataset_id))
        return dataset_map

    def analysis_map(self, claim_ids: list[str]) -> dict[str, list[UUID]]:
        analysis_map: dict[str, list[UUID]] = defaultdict(list)
        if not claim_ids:
            return analysis_map
        rows = self._session.scalars(
            select(ClaimAnalysisModel).where(ClaimAnalysisModel.claim_id.in_(claim_ids))
        )
        for row in rows:
            analysis_map[str(row.claim_id)].append(ensure_uuid(row.analysis_id))
        return analysis_map

    def question_map(self, claim_ids: list[str]) -> dict[str, list[UUID]]:
        question_map: dict[str, list[UUID]] = defaultdict(list)
        if not claim_ids:
            return question_map
        rows = self._session.scalars(
            select(ClaimQuestionModel).where(ClaimQuestionModel.claim_id.in_(claim_ids))
        )
        for row in rows:
            question_map[str(row.claim_id)].append(ensure_uuid(row.question_id))
        return question_map

    def claims_from_rows(self, rows: list[ClaimModel]) -> list[Claim]:
        claim_ids = [row.claim_id for row in rows]
        dataset_map = self.dataset_map(claim_ids)
        analysis_map = self.analysis_map(claim_ids)
        question_map = self.question_map(claim_ids)
        return [
            claim_from_model(
                row,
                supported_by_dataset_ids=dataset_map.get(str(row.claim_id), []),
                supported_by_analysis_ids=analysis_map.get(str(row.claim_id), []),
                answers_question_ids=question_map.get(str(row.claim_id), []),
            )
            for row in rows
        ]

    def get(self, entity_id: UUID) -> Claim | None:
        self._session.flush()
        row = self._session.get(ClaimModel, str(entity_id))
        if row is None:
            return None
        return self.claims_from_rows([row])[0]

    def list(self) -> list[Claim]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(ClaimModel).order_by(ClaimModel.created_at, ClaimModel.claim_id)
            )
        )
        return self.claims_from_rows(rows)

    def save(self, entity: Claim) -> None:
        entity_id = str(entity.claim_id)
        row = self._session.get(ClaimModel, entity_id)
        if row is None:
            self._session.add(claim_to_model(entity))
        else:
            apply_claim_to_model(row, entity)
        self._session.flush()
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
        replace_child_rows(
            self._session,
            ClaimQuestionModel,
            ClaimQuestionModel.claim_id,
            entity_id,
            claim_question_models(entity),
        )

    def delete(self, entity_id: UUID) -> Claim | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(ClaimModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity

    def query(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        dataset_id: UUID | None = None,
        analysis_id: UUID | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Claim], int]:
        self._session.flush()
        if project_ids is not None and not project_ids:
            return [], 0
        stmt = select(ClaimModel)
        count_stmt = select(ClaimModel.claim_id)
        if project_id is not None:
            stmt = stmt.where(ClaimModel.project_id == str(project_id))
            count_stmt = count_stmt.where(ClaimModel.project_id == str(project_id))
        if project_ids is not None:
            project_values = uuid_values(project_ids)
            stmt = stmt.where(ClaimModel.project_id.in_(project_values))
            count_stmt = count_stmt.where(ClaimModel.project_id.in_(project_values))
        if status is not None:
            stmt = stmt.where(ClaimModel.status == status)
            count_stmt = count_stmt.where(ClaimModel.status == status)
        if created_by is not None:
            created_by_clause = or_(
                ClaimModel.created_by_user_id == created_by,
                ClaimModel.created_by == created_by,
                ClaimModel.claim_id.in_(
                    select(ClaimDatasetModel.claim_id)
                    .join(
                        DatasetModel,
                        DatasetModel.dataset_id == ClaimDatasetModel.dataset_id,
                    )
                    .where(DatasetModel.created_by_user_id == created_by)
                ),
                ClaimModel.claim_id.in_(
                    select(ClaimAnalysisModel.claim_id)
                    .join(
                        AnalysisModel,
                        AnalysisModel.analysis_id == ClaimAnalysisModel.analysis_id,
                    )
                    .where(AnalysisModel.executed_by_user_id == created_by)
                ),
            )
            stmt = stmt.where(created_by_clause)
            count_stmt = count_stmt.where(created_by_clause)
        if since is not None:
            stmt = stmt.where(ClaimModel.created_at >= since)
            count_stmt = count_stmt.where(ClaimModel.created_at >= since)
        if until is not None:
            stmt = stmt.where(ClaimModel.created_at < until)
            count_stmt = count_stmt.where(ClaimModel.created_at < until)
        if dataset_id is not None:
            dataset_claim_ids = select(ClaimDatasetModel.claim_id).where(
                ClaimDatasetModel.dataset_id == str(dataset_id)
            )
            stmt = stmt.where(ClaimModel.claim_id.in_(dataset_claim_ids))
            count_stmt = count_stmt.where(ClaimModel.claim_id.in_(dataset_claim_ids))
        if analysis_id is not None:
            analysis_claim_ids = select(ClaimAnalysisModel.claim_id).where(
                ClaimAnalysisModel.analysis_id == str(analysis_id)
            )
            stmt = stmt.where(ClaimModel.claim_id.in_(analysis_claim_ids))
            count_stmt = count_stmt.where(ClaimModel.claim_id.in_(analysis_claim_ids))
        if recent_first:
            stmt = stmt.order_by(ClaimModel.created_at.desc(), ClaimModel.claim_id.desc())
        else:
            stmt = stmt.order_by(ClaimModel.created_at, ClaimModel.claim_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self.claims_from_rows(rows), total


class SQLAlchemyClaimEdgeRepository(EntityRepository[ClaimEdge]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def _edges_from_rows(self, rows: list[ClaimEdgeModel]) -> list[ClaimEdge]:
        return [claim_edge_from_model(row) for row in rows]

    def get(self, entity_id: UUID) -> ClaimEdge | None:
        self._session.flush()
        row = self._session.get(ClaimEdgeModel, str(entity_id))
        if row is None:
            return None
        return claim_edge_from_model(row)

    def list(self) -> list[ClaimEdge]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(ClaimEdgeModel).order_by(
                    ClaimEdgeModel.created_at,
                    ClaimEdgeModel.edge_id,
                )
            )
        )
        return self._edges_from_rows(rows)

    def save(self, entity: ClaimEdge) -> None:
        entity_id = str(entity.edge_id)
        row = self._session.get(ClaimEdgeModel, entity_id)
        if row is None:
            self._session.add(claim_edge_to_model(entity))
        else:
            apply_claim_edge_to_model(row, entity)
        self._session.flush()

    def delete(self, entity_id: UUID) -> ClaimEdge | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(ClaimEdgeModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity

    def query(
        self,
        *,
        project_id: UUID | None = None,
        claim_id: UUID | None = None,
        target_claim_id: UUID | None = None,
        relation: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[ClaimEdge], int]:
        self._session.flush()
        stmt = select(ClaimEdgeModel)
        count_stmt = select(ClaimEdgeModel.edge_id)
        if project_id is not None:
            stmt = stmt.join(
                ClaimModel,
                ClaimModel.claim_id == ClaimEdgeModel.claim_id,
            ).where(ClaimModel.project_id == str(project_id))
            count_stmt = count_stmt.join(
                ClaimModel,
                ClaimModel.claim_id == ClaimEdgeModel.claim_id,
            ).where(ClaimModel.project_id == str(project_id))
        if claim_id is not None:
            stmt = stmt.where(ClaimEdgeModel.claim_id == str(claim_id))
            count_stmt = count_stmt.where(ClaimEdgeModel.claim_id == str(claim_id))
        if target_claim_id is not None:
            stmt = stmt.where(ClaimEdgeModel.target_claim_id == str(target_claim_id))
            count_stmt = count_stmt.where(ClaimEdgeModel.target_claim_id == str(target_claim_id))
        if relation is not None:
            stmt = stmt.where(ClaimEdgeModel.relation == relation)
            count_stmt = count_stmt.where(ClaimEdgeModel.relation == relation)
        stmt = stmt.order_by(ClaimEdgeModel.created_at, ClaimEdgeModel.edge_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self._edges_from_rows(rows), total


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
            claim_map[str(row.viz_id)].append(ensure_uuid(row.claim_id))
        return claim_map

    def dataset_map(self, analysis_ids: list[str]) -> dict[str, list[UUID]]:
        dataset_map: dict[str, list[UUID]] = defaultdict(list)
        if not analysis_ids:
            return dataset_map
        rows = self._session.scalars(
            select(AnalysisDatasetModel).where(AnalysisDatasetModel.analysis_id.in_(analysis_ids))
        )
        for row in rows:
            dataset_map[str(row.analysis_id)].append(ensure_uuid(row.dataset_id))
        return dataset_map

    def visualizations_from_rows(self, rows: list[VisualizationModel]) -> list[Visualization]:
        visualization_ids = [row.viz_id for row in rows]
        analysis_ids = sorted({row.analysis_id for row in rows})
        claim_map = self.claim_map(visualization_ids)
        dataset_map = self.dataset_map(analysis_ids)
        return [
            visualization_from_model(
                row,
                dataset_ids=sorted(dataset_map.get(str(row.analysis_id), []), key=str),
                related_claim_ids=claim_map.get(str(row.viz_id), []),
            )
            for row in rows
        ]

    def get(self, entity_id: UUID) -> Visualization | None:
        self._session.flush()
        row = self._session.get(VisualizationModel, str(entity_id))
        if row is None:
            return None
        return self.visualizations_from_rows([row])[0]

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
        return self.visualizations_from_rows(rows)

    def save(self, entity: Visualization) -> None:
        entity_id = str(entity.viz_id)
        row = self._session.get(VisualizationModel, entity_id)
        if row is None:
            self._session.add(visualization_to_model(entity))
        else:
            apply_visualization_to_model(row, entity)
        self._session.flush()
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

    def query(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        analysis_id: UUID | None = None,
        claim_id: UUID | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Visualization], int]:
        self._session.flush()
        if project_ids is not None and not project_ids:
            return [], 0
        stmt = select(VisualizationModel)
        count_stmt = select(VisualizationModel.viz_id)
        distinct_required = False
        joined_analysis = False
        if project_id is not None:
            stmt = stmt.join(
                AnalysisModel,
                AnalysisModel.analysis_id == VisualizationModel.analysis_id,
            ).where(AnalysisModel.project_id == str(project_id))
            count_stmt = count_stmt.join(
                AnalysisModel,
                AnalysisModel.analysis_id == VisualizationModel.analysis_id,
            ).where(AnalysisModel.project_id == str(project_id))
            joined_analysis = True
        if project_ids is not None:
            if not joined_analysis:
                stmt = stmt.join(
                    AnalysisModel,
                    AnalysisModel.analysis_id == VisualizationModel.analysis_id,
                )
                count_stmt = count_stmt.join(
                    AnalysisModel,
                    AnalysisModel.analysis_id == VisualizationModel.analysis_id,
                )
                joined_analysis = True
            project_values = uuid_values(project_ids)
            stmt = stmt.where(AnalysisModel.project_id.in_(project_values))
            count_stmt = count_stmt.where(AnalysisModel.project_id.in_(project_values))
        if analysis_id is not None:
            stmt = stmt.where(VisualizationModel.analysis_id == str(analysis_id))
            count_stmt = count_stmt.where(VisualizationModel.analysis_id == str(analysis_id))
        if claim_id is not None:
            distinct_required = True
            stmt = stmt.join(
                VisualizationClaimModel,
                VisualizationClaimModel.viz_id == VisualizationModel.viz_id,
            ).where(VisualizationClaimModel.claim_id == str(claim_id))
            count_stmt = count_stmt.join(
                VisualizationClaimModel,
                VisualizationClaimModel.viz_id == VisualizationModel.viz_id,
            ).where(VisualizationClaimModel.claim_id == str(claim_id))
        if created_by is not None:
            stmt = stmt.where(VisualizationModel.created_by_user_id == created_by)
            count_stmt = count_stmt.where(VisualizationModel.created_by_user_id == created_by)
        if since is not None:
            stmt = stmt.where(VisualizationModel.created_at >= since)
            count_stmt = count_stmt.where(VisualizationModel.created_at >= since)
        if until is not None:
            stmt = stmt.where(VisualizationModel.created_at < until)
            count_stmt = count_stmt.where(VisualizationModel.created_at < until)
        if distinct_required:
            stmt = stmt.distinct()
            count_stmt = count_stmt.distinct()
        if recent_first:
            stmt = stmt.order_by(
                VisualizationModel.created_at.desc(),
                VisualizationModel.viz_id.desc(),
            )
        else:
            stmt = stmt.order_by(VisualizationModel.created_at, VisualizationModel.viz_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self.visualizations_from_rows(rows), total
