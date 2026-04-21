"""Dataset and dataset-review SQLAlchemy repositories."""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import DatasetModel, DatasetQuestionLinkModel, DatasetReviewModel
from lab_tracker.models import Dataset, DatasetReview
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mappers import (
    apply_dataset_review_to_model,
    apply_dataset_to_model,
    dataset_from_model,
    dataset_question_link_from_model,
    dataset_question_link_models,
    dataset_review_from_model,
    dataset_review_to_model,
    dataset_to_model,
)

from .common import SQLAlchemyModelRepository, replace_child_rows


class SQLAlchemyDatasetRepository(EntityRepository[Dataset]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def link_map(self, dataset_ids: list[str]) -> dict[str, list[DatasetQuestionLinkModel]]:
        link_map: dict[str, list[DatasetQuestionLinkModel]] = defaultdict(list)
        if not dataset_ids:
            return link_map
        rows = self._session.scalars(
            select(DatasetQuestionLinkModel).where(
                DatasetQuestionLinkModel.dataset_id.in_(dataset_ids)
            )
        )
        for row in rows:
            link_map[row.dataset_id].append(row)
        return link_map

    def get(self, entity_id: UUID) -> Dataset | None:
        self._session.flush()
        row = self._session.get(DatasetModel, str(entity_id))
        if row is None:
            return None
        links = self.link_map([row.dataset_id]).get(row.dataset_id, [])
        question_links = [dataset_question_link_from_model(link) for link in links]
        return dataset_from_model(row, question_links=question_links)

    def list(self) -> list[Dataset]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(DatasetModel).order_by(DatasetModel.created_at, DatasetModel.dataset_id)
            )
        )
        dataset_ids = [row.dataset_id for row in rows]
        link_map = self.link_map(dataset_ids)
        entities: list[Dataset] = []
        for row in rows:
            link_rows = link_map.get(row.dataset_id, [])
            question_links = [dataset_question_link_from_model(link) for link in link_rows]
            entities.append(dataset_from_model(row, question_links=question_links))
        return entities

    def save(self, entity: Dataset) -> None:
        entity_id = str(entity.dataset_id)
        row = self._session.get(DatasetModel, entity_id)
        if row is None:
            self._session.add(dataset_to_model(entity))
        else:
            apply_dataset_to_model(row, entity)
        replace_child_rows(
            self._session,
            DatasetQuestionLinkModel,
            DatasetQuestionLinkModel.dataset_id,
            entity_id,
            dataset_question_link_models(entity),
        )

    def delete(self, entity_id: UUID) -> Dataset | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(DatasetModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity


class SQLAlchemyDatasetReviewRepository(
    SQLAlchemyModelRepository[DatasetReview, DatasetReviewModel]
):
    def __init__(self, session: OrmSession) -> None:
        super().__init__(
            session,
            model_type=DatasetReviewModel,
            id_column=DatasetReviewModel.requested_at,
            entity_id_getter=lambda entity: entity.review_id,
            to_model=dataset_review_to_model,
            from_model=dataset_review_from_model,
            apply_to_model=apply_dataset_review_to_model,
        )
