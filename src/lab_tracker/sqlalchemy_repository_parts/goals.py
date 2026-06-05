"""Goal SQLAlchemy repository."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import GoalLinkModel, GoalModel
from lab_tracker.models import Goal, GoalLink
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mappers import (
    apply_goal_to_model,
    goal_from_model,
    goal_link_from_model,
    goal_link_models,
    goal_to_model,
)

from .common import apply_pagination, count_from_statement, replace_child_rows


class SQLAlchemyGoalRepository(EntityRepository[Goal]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def link_map(self, goal_ids: list[str]) -> dict[str, list[GoalLink]]:
        link_map: dict[str, list[GoalLink]] = {}
        if not goal_ids:
            return link_map
        rows = list(
            self._session.scalars(
                select(GoalLinkModel)
                .where(GoalLinkModel.goal_id.in_(goal_ids))
                .order_by(GoalLinkModel.created_at, GoalLinkModel.link_id)
            )
        )
        for row in rows:
            link_map.setdefault(row.goal_id, []).append(goal_link_from_model(row))
        return link_map

    def goals_from_rows(self, rows: list[GoalModel]) -> list[Goal]:
        goal_ids = [row.goal_id for row in rows]
        links = self.link_map(goal_ids)
        return [goal_from_model(row, links=links.get(row.goal_id, [])) for row in rows]

    def get(self, entity_id: UUID) -> Goal | None:
        self._session.flush()
        row = self._session.get(GoalModel, str(entity_id))
        if row is None:
            return None
        return self.goals_from_rows([row])[0]

    def list(self) -> list[Goal]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(GoalModel).order_by(GoalModel.created_at, GoalModel.goal_id)
            )
        )
        return self.goals_from_rows(rows)

    def save(self, entity: Goal) -> None:
        entity_id = str(entity.goal_id)
        row = self._session.get(GoalModel, entity_id)
        if row is None:
            self._session.add(goal_to_model(entity))
        else:
            apply_goal_to_model(row, entity)
        replace_child_rows(
            self._session,
            GoalLinkModel,
            GoalLinkModel.goal_id,
            entity_id,
            goal_link_models(entity),
        )

    def delete(self, entity_id: UUID) -> Goal | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(GoalModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity

    def query(
        self,
        *,
        project_id: UUID | None = None,
        goal_type: str | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Goal], int]:
        self._session.flush()
        stmt = select(GoalModel)
        count_stmt = select(GoalModel.goal_id)
        distinct_required = False
        if project_id is not None:
            stmt = stmt.where(GoalModel.project_id == str(project_id))
            count_stmt = count_stmt.where(GoalModel.project_id == str(project_id))
        if goal_type is not None:
            stmt = stmt.where(GoalModel.goal_type == goal_type)
            count_stmt = count_stmt.where(GoalModel.goal_type == goal_type)
        if status is not None:
            stmt = stmt.where(GoalModel.status == status)
            count_stmt = count_stmt.where(GoalModel.status == status)
        if target_entity_type is not None and target_entity_id is not None:
            distinct_required = True
            stmt = stmt.join(GoalLinkModel, GoalLinkModel.goal_id == GoalModel.goal_id).where(
                GoalLinkModel.entity_type == target_entity_type,
                GoalLinkModel.entity_id == str(target_entity_id),
            )
            count_stmt = count_stmt.join(
                GoalLinkModel,
                GoalLinkModel.goal_id == GoalModel.goal_id,
            ).where(
                GoalLinkModel.entity_type == target_entity_type,
                GoalLinkModel.entity_id == str(target_entity_id),
            )
        if distinct_required:
            stmt = stmt.distinct()
            count_stmt = count_stmt.distinct()
        stmt = stmt.order_by(GoalModel.created_at, GoalModel.goal_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self.goals_from_rows(rows), total

    def query_links(
        self,
        *,
        goal_id: UUID | None = None,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        link_status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[GoalLink], int]:
        self._session.flush()
        stmt = select(GoalLinkModel)
        count_stmt = select(GoalLinkModel.link_id)
        if goal_id is not None:
            stmt = stmt.where(GoalLinkModel.goal_id == str(goal_id))
            count_stmt = count_stmt.where(GoalLinkModel.goal_id == str(goal_id))
        if entity_type is not None:
            stmt = stmt.where(GoalLinkModel.entity_type == entity_type)
            count_stmt = count_stmt.where(GoalLinkModel.entity_type == entity_type)
        if entity_id is not None:
            stmt = stmt.where(GoalLinkModel.entity_id == str(entity_id))
            count_stmt = count_stmt.where(GoalLinkModel.entity_id == str(entity_id))
        if link_status is not None:
            stmt = stmt.where(GoalLinkModel.link_status == link_status)
            count_stmt = count_stmt.where(GoalLinkModel.link_status == link_status)
        stmt = stmt.order_by(GoalLinkModel.created_at, GoalLinkModel.link_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [goal_link_from_model(row) for row in rows], total
