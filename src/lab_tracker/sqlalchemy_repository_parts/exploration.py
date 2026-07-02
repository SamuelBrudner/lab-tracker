"""SQLAlchemy repository for exploration trajectory nodes."""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import ExplorationNodeEdgeModel, ExplorationNodeModel
from lab_tracker.db_types import ensure_uuid
from lab_tracker.models import ExplorationNode
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mappers import (
    apply_exploration_node_to_model,
    exploration_node_edge_models,
    exploration_node_from_model,
    exploration_node_to_model,
)

from .common import apply_pagination, count_from_statement, replace_child_rows, uuid_values


class SQLAlchemyExplorationNodeRepository(EntityRepository[ExplorationNode]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def edge_maps(self, node_ids: list[str]) -> tuple[dict[str, list[UUID]], dict[str, list[UUID]]]:
        parent_map: dict[str, list[UUID]] = defaultdict(list)
        dependency_map: dict[str, list[UUID]] = defaultdict(list)
        if not node_ids:
            return parent_map, dependency_map
        rows = self._session.scalars(
            select(ExplorationNodeEdgeModel).where(
                ExplorationNodeEdgeModel.target_node_id.in_(node_ids)
            )
        )
        for row in rows:
            if row.relation == "parent":
                parent_map[str(row.target_node_id)].append(ensure_uuid(row.source_node_id))
            elif row.relation == "also_depends_on":
                dependency_map[str(row.target_node_id)].append(ensure_uuid(row.source_node_id))
        return parent_map, dependency_map

    def nodes_from_rows(self, rows: list[ExplorationNodeModel]) -> list[ExplorationNode]:
        node_ids = [row.node_id for row in rows]
        parent_map, dependency_map = self.edge_maps(node_ids)
        return [
            exploration_node_from_model(
                row,
                parent_node_ids=parent_map.get(str(row.node_id), []),
                also_depends_on_node_ids=dependency_map.get(str(row.node_id), []),
            )
            for row in rows
        ]

    def get(self, entity_id: UUID) -> ExplorationNode | None:
        self._session.flush()
        row = self._session.get(ExplorationNodeModel, str(entity_id))
        if row is None:
            return None
        return self.nodes_from_rows([row])[0]

    def list(self) -> list[ExplorationNode]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(ExplorationNodeModel).order_by(
                    ExplorationNodeModel.created_at,
                    ExplorationNodeModel.node_id,
                )
            )
        )
        return self.nodes_from_rows(rows)

    def save(self, entity: ExplorationNode) -> None:
        entity_id = str(entity.node_id)
        row = self._session.get(ExplorationNodeModel, entity_id)
        if row is None:
            self._session.add(exploration_node_to_model(entity))
        else:
            apply_exploration_node_to_model(row, entity)
        self._session.flush()
        replace_child_rows(
            self._session,
            ExplorationNodeEdgeModel,
            ExplorationNodeEdgeModel.target_node_id,
            entity_id,
            exploration_node_edge_models(entity),
        )

    def delete(self, entity_id: UUID) -> ExplorationNode | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(ExplorationNodeModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity

    def query(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        node_type: str | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        created_by: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[ExplorationNode], int]:
        self._session.flush()
        if project_ids is not None and not project_ids:
            return [], 0
        stmt = select(ExplorationNodeModel)
        count_stmt = select(ExplorationNodeModel.node_id)
        if project_id is not None:
            stmt = stmt.where(ExplorationNodeModel.project_id == str(project_id))
            count_stmt = count_stmt.where(ExplorationNodeModel.project_id == str(project_id))
        if project_ids is not None:
            project_values = uuid_values(project_ids)
            stmt = stmt.where(ExplorationNodeModel.project_id.in_(project_values))
            count_stmt = count_stmt.where(ExplorationNodeModel.project_id.in_(project_values))
        if node_type is not None:
            stmt = stmt.where(ExplorationNodeModel.node_type == node_type)
            count_stmt = count_stmt.where(ExplorationNodeModel.node_type == node_type)
        if status is not None:
            stmt = stmt.where(ExplorationNodeModel.status == status)
            count_stmt = count_stmt.where(ExplorationNodeModel.status == status)
        if target_entity_type is not None:
            stmt = stmt.where(ExplorationNodeModel.target_entity_type == target_entity_type)
            count_stmt = count_stmt.where(
                ExplorationNodeModel.target_entity_type == target_entity_type
            )
        if target_entity_id is not None:
            stmt = stmt.where(ExplorationNodeModel.target_entity_id == str(target_entity_id))
            count_stmt = count_stmt.where(
                ExplorationNodeModel.target_entity_id == str(target_entity_id)
            )
        if created_by is not None:
            created_by_clause = or_(
                ExplorationNodeModel.created_by_user_id == created_by,
                ExplorationNodeModel.created_by == created_by,
            )
            stmt = stmt.where(created_by_clause)
            count_stmt = count_stmt.where(created_by_clause)
        if recent_first:
            stmt = stmt.order_by(
                ExplorationNodeModel.created_at.desc(),
                ExplorationNodeModel.node_id.desc(),
            )
        else:
            stmt = stmt.order_by(
                ExplorationNodeModel.created_at,
                ExplorationNodeModel.node_id,
            )
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self.nodes_from_rows(rows), total
