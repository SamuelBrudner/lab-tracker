"""Dataset SQLAlchemy repository."""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import (
    DatasetFileModel,
    DatasetModel,
    DatasetQuestionLinkModel,
    NoteTargetModel,
)
from lab_tracker.models import Dataset, DatasetFile
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mappers import (
    apply_dataset_to_model,
    dataset_from_model,
    dataset_question_link_from_model,
    dataset_question_link_models,
    dataset_to_model,
)

from .common import apply_pagination, count_from_statement, replace_child_rows


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

    def datasets_from_rows(self, rows: list[DatasetModel]) -> list[Dataset]:
        dataset_ids = [row.dataset_id for row in rows]
        link_map = self.link_map(dataset_ids)
        return [
            dataset_from_model(
                row,
                question_links=[
                    dataset_question_link_from_model(link)
                    for link in link_map.get(row.dataset_id, [])
                ],
            )
            for row in rows
        ]

    def get(self, entity_id: UUID) -> Dataset | None:
        self._session.flush()
        row = self._session.get(DatasetModel, str(entity_id))
        if row is None:
            return None
        return self.datasets_from_rows([row])[0]

    def list(self) -> list[Dataset]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(DatasetModel).order_by(DatasetModel.created_at, DatasetModel.dataset_id)
            )
        )
        return self.datasets_from_rows(rows)

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

    def query(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Dataset], int]:
        self._session.flush()
        stmt = select(DatasetModel)
        count_stmt = select(DatasetModel.dataset_id)
        if project_id is not None:
            stmt = stmt.where(DatasetModel.project_id == str(project_id))
            count_stmt = count_stmt.where(DatasetModel.project_id == str(project_id))
        if status is not None:
            stmt = stmt.where(DatasetModel.status == status)
            count_stmt = count_stmt.where(DatasetModel.status == status)
        stmt = stmt.order_by(DatasetModel.created_at, DatasetModel.dataset_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self.datasets_from_rows(rows), total

    def query_files(
        self,
        *,
        dataset_id: UUID,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[DatasetFile], int]:
        self._session.flush()
        stmt = select(DatasetFileModel).where(DatasetFileModel.dataset_id == str(dataset_id))
        count_stmt = select(DatasetFileModel.file_id).where(
            DatasetFileModel.dataset_id == str(dataset_id)
        )
        stmt = stmt.order_by(DatasetFileModel.created_at, DatasetFileModel.file_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return (
            [
                DatasetFile(
                    file_id=UUID(row.file_id),
                    path=row.path,
                    checksum=row.checksum,
                    size_bytes=row.size_bytes,
                )
                for row in rows
            ],
            total,
        )

    def list_file_entities(self, dataset_id: UUID) -> list[DatasetFile]:
        files, _ = self.query_files(dataset_id=dataset_id, limit=None, offset=0)
        return files

    def list_note_target_ids(self, dataset_id: UUID) -> list[UUID]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(NoteTargetModel.note_id).where(
                    NoteTargetModel.entity_type == "dataset",
                    NoteTargetModel.entity_id == str(dataset_id),
                )
            )
        )
        return [UUID(note_id) for note_id in rows]
