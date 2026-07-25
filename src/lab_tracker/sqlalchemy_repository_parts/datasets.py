"""Dataset SQLAlchemy repository."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.collection_db_models import DatasetCollectionSnapshotLinkModel
from lab_tracker.collection_models import (
    DatasetCollectionSnapshotReference,
    DatasetSummary,
)
from lab_tracker.db_models import (
    DatasetFileModel,
    DatasetModel,
    DatasetQuestionLinkModel,
    NoteTargetModel,
)
from lab_tracker.db_types import ensure_uuid
from lab_tracker.models import Dataset, DatasetFile
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mappers import (
    apply_dataset_to_model,
    dataset_from_model,
    dataset_question_link_from_model,
    dataset_question_link_models,
    dataset_to_model,
)

from .common import apply_pagination, count_from_statement, replace_child_rows, uuid_values


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
            link_map[str(row.dataset_id)].append(row)
        return link_map

    def datasets_from_rows(self, rows: list[DatasetModel]) -> list[Dataset]:
        dataset_ids = [row.dataset_id for row in rows]
        link_map = self.link_map(dataset_ids)
        return [
            dataset_from_model(
                row,
                question_links=[
                    dataset_question_link_from_model(link)
                    for link in link_map.get(str(row.dataset_id), [])
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
        self._session.flush()
        replace_child_rows(
            self._session,
            DatasetQuestionLinkModel,
            DatasetQuestionLinkModel.dataset_id,
            entity_id,
            dataset_question_link_models(entity),
        )
        replace_child_rows(
            self._session,
            DatasetCollectionSnapshotLinkModel,
            DatasetCollectionSnapshotLinkModel.dataset_id,
            entity_id,
            [
                DatasetCollectionSnapshotLinkModel(
                    dataset_id=entity.dataset_id,
                    snapshot_id=snapshot.snapshot_id,
                )
                for snapshot in entity.commit_manifest.collection_snapshots
            ],
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
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Dataset], int]:
        self._session.flush()
        if project_ids is not None and not project_ids:
            return [], 0
        stmt = select(DatasetModel)
        count_stmt = select(DatasetModel.dataset_id)
        if project_id is not None:
            stmt = stmt.where(DatasetModel.project_id == str(project_id))
            count_stmt = count_stmt.where(DatasetModel.project_id == str(project_id))
        if project_ids is not None:
            project_values = uuid_values(project_ids)
            stmt = stmt.where(DatasetModel.project_id.in_(project_values))
            count_stmt = count_stmt.where(DatasetModel.project_id.in_(project_values))
        if status is not None:
            stmt = stmt.where(DatasetModel.status == status)
            count_stmt = count_stmt.where(DatasetModel.status == status)
        if created_by is not None:
            stmt = stmt.where(DatasetModel.created_by_user_id == created_by)
            count_stmt = count_stmt.where(DatasetModel.created_by_user_id == created_by)
        if since is not None:
            stmt = stmt.where(DatasetModel.created_at >= since)
            count_stmt = count_stmt.where(DatasetModel.created_at >= since)
        if until is not None:
            stmt = stmt.where(DatasetModel.created_at < until)
            count_stmt = count_stmt.where(DatasetModel.created_at < until)
        if recent_first:
            stmt = stmt.order_by(
                DatasetModel.created_at.desc(),
                DatasetModel.dataset_id.desc(),
            )
        else:
            stmt = stmt.order_by(DatasetModel.created_at, DatasetModel.dataset_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self.datasets_from_rows(rows), total

    def query_summaries(
        self,
        *,
        dataset_id: UUID | None = None,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[DatasetSummary], int]:
        self._session.flush()
        if project_ids is not None and not project_ids:
            return [], 0
        stmt = select(
            DatasetModel.dataset_id,
            DatasetModel.project_id,
            DatasetModel.commit_hash,
            DatasetModel.primary_question_id,
            DatasetModel.status,
            DatasetModel.manifest_source_session_id,
            DatasetModel.manifest_collection_snapshots,
            DatasetModel.created_at,
            DatasetModel.updated_at,
            func.coalesce(
                func.json_array_length(DatasetModel.manifest_files),
                0,
            ).label("file_count"),
            func.coalesce(
                func.json_array_length(
                    DatasetModel.manifest_external_artifacts
                ),
                0,
            ).label("external_artifact_count"),
        )
        count_stmt = select(DatasetModel.dataset_id)
        filters = []
        if dataset_id is not None:
            filters.append(DatasetModel.dataset_id == str(dataset_id))
        if project_id is not None:
            filters.append(DatasetModel.project_id == str(project_id))
        if project_ids is not None:
            filters.append(
                DatasetModel.project_id.in_(uuid_values(project_ids))
            )
        if status is not None:
            filters.append(DatasetModel.status == status)
        if created_by is not None:
            filters.append(DatasetModel.created_by_user_id == created_by)
        if since is not None:
            filters.append(DatasetModel.created_at >= since)
        if until is not None:
            filters.append(DatasetModel.created_at < until)
        if filters:
            stmt = stmt.where(*filters)
            count_stmt = count_stmt.where(*filters)
        if recent_first:
            stmt = stmt.order_by(
                DatasetModel.created_at.desc(),
                DatasetModel.dataset_id.desc(),
            )
        else:
            stmt = stmt.order_by(
                DatasetModel.created_at,
                DatasetModel.dataset_id,
            )
        total = count_from_statement(self._session, count_stmt)
        rows = list(
            self._session.execute(
                apply_pagination(stmt, limit=limit, offset=offset)
            )
        )
        link_map = self.link_map([row.dataset_id for row in rows])
        summaries: list[DatasetSummary] = []
        for row in rows:
            collection_snapshots = [
                DatasetCollectionSnapshotReference.model_validate(item)
                for item in (row.manifest_collection_snapshots or [])
            ]
            resolved_question_links = [
                dataset_question_link_from_model(link)
                for link in link_map.get(str(row.dataset_id), [])
            ]
            resolved_question_links.sort(
                key=lambda link: (
                    link.role.value != "primary",
                    str(link.question_id),
                )
            )
            question_links = [
                link.model_dump(mode="json")
                for link in resolved_question_links
            ]
            summaries.append(
                DatasetSummary(
                    dataset_id=row.dataset_id,
                    project_id=row.project_id,
                    commit_hash=row.commit_hash,
                    primary_question_id=row.primary_question_id,
                    question_links=question_links,
                    status=row.status.value,
                    source_session_id=row.manifest_source_session_id,
                    file_count=int(row.file_count or 0),
                    external_artifact_count=int(
                        row.external_artifact_count or 0
                    ),
                    collection_count=len(collection_snapshots),
                    collection_member_count=sum(
                        snapshot.member_count
                        for snapshot in collection_snapshots
                    ),
                    collection_total_size_bytes=sum(
                        snapshot.total_size_bytes
                        for snapshot in collection_snapshots
                    ),
                    collection_snapshots=collection_snapshots,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
            )
        return summaries, total

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
                    file_id=ensure_uuid(row.file_id),
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
        return [ensure_uuid(note_id) for note_id in rows]
