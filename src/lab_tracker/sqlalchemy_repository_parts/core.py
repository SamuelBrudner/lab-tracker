"""Project and question SQLAlchemy repositories."""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy import select
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

from .common import SQLAlchemyModelRepository, replace_child_rows


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
        parent_ids = self.parent_map([row.question_id]).get(row.question_id, [])
        return question_from_model(row, parent_question_ids=parent_ids)

    def list(self) -> list[Question]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(QuestionModel).order_by(QuestionModel.created_at, QuestionModel.question_id)
            )
        )
        question_ids = [row.question_id for row in rows]
        parent_map = self.parent_map(question_ids)
        return [
            question_from_model(row, parent_question_ids=parent_map.get(row.question_id, []))
            for row in rows
        ]

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
