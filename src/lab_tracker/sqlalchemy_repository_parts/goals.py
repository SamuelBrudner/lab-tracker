"""Goal SQLAlchemy repository."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import aliased

from lab_tracker.db_models import GoalLinkModel, GoalModel
from lab_tracker.models import EntityType, Goal, GoalLink
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
            link_map.setdefault(str(row.goal_id), []).append(goal_link_from_model(row))
        return link_map

    def goals_from_rows(self, rows: list[GoalModel]) -> list[Goal]:
        goal_ids = [row.goal_id for row in rows]
        links = self.link_map(goal_ids)
        return [goal_from_model(row, links=links.get(str(row.goal_id), [])) for row in rows]

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
        self._session.flush()
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
        project_ids: set[UUID] | None = None,
        goal_type: str | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        target_entity_keys: set[tuple[str, UUID]] | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Goal], int]:
        self._session.flush()
        stmt = select(GoalModel)
        count_stmt = select(GoalModel.goal_id)
        if project_id is not None:
            project_clause = self._project_scope_clause({project_id})
            stmt = stmt.where(project_clause)
            count_stmt = count_stmt.where(project_clause)
        elif project_ids is not None:
            if not project_ids:
                return [], 0
            project_clause = self._project_scope_clause(project_ids)
            stmt = stmt.where(project_clause)
            count_stmt = count_stmt.where(project_clause)
        if goal_type is not None:
            stmt = stmt.where(GoalModel.goal_type == goal_type)
            count_stmt = count_stmt.where(GoalModel.goal_type == goal_type)
        if status is not None:
            stmt = stmt.where(GoalModel.status == status)
            count_stmt = count_stmt.where(GoalModel.status == status)
        target_values = _target_values(
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
            target_entity_keys=target_entity_keys,
        )
        if target_values is not None and not target_values:
            return [], 0
        if target_values:
            matching_goal_ids = (
                select(GoalLinkModel.goal_id)
                .where(
                    tuple_(GoalLinkModel.entity_type, GoalLinkModel.entity_id).in_(
                        target_values
                    )
                )
                .distinct()
            )
            stmt = stmt.where(GoalModel.goal_id.in_(matching_goal_ids))
            count_stmt = count_stmt.where(GoalModel.goal_id.in_(matching_goal_ids))
        if recent_first:
            stmt = stmt.order_by(GoalModel.created_at.desc(), GoalModel.goal_id.desc())
        else:
            stmt = stmt.order_by(GoalModel.created_at, GoalModel.goal_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self.goals_from_rows(rows), total

    def _project_scope_clause(self, project_ids: set[UUID]):
        project_values = [str(project_id) for project_id in sorted(project_ids, key=str)]
        project_link = aliased(GoalLinkModel)
        project_link_goals = select(project_link.goal_id).where(
            project_link.entity_type == EntityType.PROJECT.value,
            project_link.entity_id.in_(project_values),
        )
        return or_(
            GoalModel.project_id.in_(project_values),
            and_(
                GoalModel.project_id.is_(None),
                GoalModel.goal_id.in_(project_link_goals),
            ),
        )

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


def _target_values(
    *,
    target_entity_type: str | None,
    target_entity_id: UUID | None,
    target_entity_keys: set[tuple[str, UUID]] | None,
) -> list[tuple[str, str]] | None:
    values: set[tuple[str, str]] = set()
    if target_entity_type is not None and target_entity_id is not None:
        values.add((target_entity_type, str(target_entity_id)))
    if target_entity_keys is not None:
        values.update(
            (entity_type, str(entity_id))
            for entity_type, entity_id in target_entity_keys
        )
        if not values:
            return []
    if not values:
        return None
    return sorted(values)
