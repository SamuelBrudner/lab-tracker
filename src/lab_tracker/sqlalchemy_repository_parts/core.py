"""Project and question SQLAlchemy repositories."""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import ProjectModel, QuestionModel, QuestionParentModel
from lab_tracker.models import Project, Question
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mappers import (
    apply_project_to_model,
    apply_question_to_model,
    project_from_model,
    project_to_model,
    question_from_model,
    question_parent_models,
    question_to_model,
)

from .common import (
    SQLAlchemyModelRepository,
    apply_pagination,
    count_from_statement,
    replace_child_rows,
    substring_pattern,
)


class SQLAlchemyProjectRepository(SQLAlchemyModelRepository[Project, ProjectModel]):
    def __init__(self, session: OrmSession) -> None:
        super().__init__(
            session,
            model_type=ProjectModel,
            id_column=ProjectModel.project_id,
            entity_id_getter=lambda entity: entity.project_id,
            to_model=project_to_model,
            from_model=project_from_model,
            apply_to_model=apply_project_to_model,
        )

    def query(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Project], int]:
        self._session.flush()
        stmt = select(ProjectModel)
        count_stmt = select(ProjectModel.project_id)
        if status is not None:
            stmt = stmt.where(ProjectModel.status == status)
            count_stmt = count_stmt.where(ProjectModel.status == status)
        stmt = stmt.order_by(ProjectModel.created_at, ProjectModel.project_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [project_from_model(row) for row in rows], total


class SQLAlchemyQuestionRepository(EntityRepository[Question]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def parent_map(self, question_ids: list[str]) -> dict[str, list[UUID]]:
        parent_map: dict[str, list[UUID]] = defaultdict(list)
        if not question_ids:
            return parent_map
        rows = self._session.scalars(
            select(QuestionParentModel).where(QuestionParentModel.question_id.in_(question_ids))
        )
        for row in rows:
            parent_map[row.question_id].append(UUID(row.parent_question_id))
        return parent_map

    def get(self, entity_id: UUID) -> Question | None:
        self._session.flush()
        row = self._session.get(QuestionModel, str(entity_id))
        if row is None:
            return None
        return self.questions_from_rows([row])[0]

    def list(self) -> list[Question]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(QuestionModel).order_by(QuestionModel.created_at, QuestionModel.question_id)
            )
        )
        return self.questions_from_rows(rows)

    def questions_from_rows(self, rows: list[QuestionModel]) -> list[Question]:
        question_ids = [row.question_id for row in rows]
        parent_map = self.parent_map(question_ids)
        return [
            question_from_model(row, parent_question_ids=parent_map.get(row.question_id, []))
            for row in rows
        ]

    def descendant_question_cte(self, ancestor_question_id: UUID):
        descendants = (
            select(QuestionParentModel.question_id.label("question_id"))
            .where(QuestionParentModel.parent_question_id == str(ancestor_question_id))
            .cte(name="descendant_questions", recursive=True)
        )
        descendants = descendants.union_all(
            select(QuestionParentModel.question_id).join(
                descendants,
                QuestionParentModel.parent_question_id == descendants.c.question_id,
            )
        )
        return descendants

    def query(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        question_type: str | None = None,
        search: str | None = None,
        parent_question_id: UUID | None = None,
        ancestor_question_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Question], int]:
        self._session.flush()
        stmt = select(QuestionModel)
        count_stmt = select(QuestionModel.question_id)
        if project_id is not None:
            stmt = stmt.where(QuestionModel.project_id == str(project_id))
            count_stmt = count_stmt.where(QuestionModel.project_id == str(project_id))
        if status is not None:
            stmt = stmt.where(QuestionModel.status == status)
            count_stmt = count_stmt.where(QuestionModel.status == status)
        if question_type is not None:
            stmt = stmt.where(QuestionModel.question_type == question_type)
            count_stmt = count_stmt.where(QuestionModel.question_type == question_type)
        pattern = substring_pattern(search)
        if pattern is not None:
            search_clause = or_(
                QuestionModel.text.ilike(pattern, escape="\\"),
                QuestionModel.hypothesis.ilike(pattern, escape="\\"),
            )
            stmt = stmt.where(search_clause)
            count_stmt = count_stmt.where(search_clause)
        if parent_question_id is not None:
            stmt = stmt.join(
                QuestionParentModel,
                QuestionParentModel.question_id == QuestionModel.question_id,
            ).where(QuestionParentModel.parent_question_id == str(parent_question_id))
            count_stmt = count_stmt.join(
                QuestionParentModel,
                QuestionParentModel.question_id == QuestionModel.question_id,
            ).where(QuestionParentModel.parent_question_id == str(parent_question_id))
        if ancestor_question_id is not None:
            descendants = self.descendant_question_cte(ancestor_question_id)
            stmt = stmt.where(QuestionModel.question_id.in_(select(descendants.c.question_id)))
            count_stmt = count_stmt.where(
                QuestionModel.question_id.in_(select(descendants.c.question_id))
            )
        stmt = stmt.order_by(QuestionModel.created_at, QuestionModel.question_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self.questions_from_rows(rows), total

    def save(self, entity: Question) -> None:
        entity_id = str(entity.question_id)
        row = self._session.get(QuestionModel, entity_id)
        if row is None:
            self._session.add(question_to_model(entity))
        else:
            apply_question_to_model(row, entity)
        replace_child_rows(
            self._session,
            QuestionParentModel,
            QuestionParentModel.question_id,
            entity_id,
            question_parent_models(entity),
        )

    def delete(self, entity_id: UUID) -> Question | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(QuestionModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity
