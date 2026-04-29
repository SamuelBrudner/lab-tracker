"""Graph draft SQLAlchemy repository."""

from __future__ import annotations

from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import GraphChangeOperationModel, GraphChangeSetModel
from lab_tracker.models import (
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
)
from lab_tracker.repository import EntityRepository

from .common import apply_pagination, count_from_statement, replace_child_rows


def _uuid(value: str | None) -> UUID | None:
    return UUID(value) if value else None


def _uuid_str(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _dict(value: Any) -> dict[str, Any]:
    return dict(value or {})


def _list(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    return [dict(item) for item in value]


def operation_to_model(operation: GraphChangeOperation) -> GraphChangeOperationModel:
    return GraphChangeOperationModel(
        operation_id=str(operation.operation_id),
        change_set_id=str(operation.change_set_id),
        sequence=operation.sequence,
        op=operation.op.value,
        entity_type=operation.entity_type.value,
        target_entity_id=_uuid_str(operation.target_entity_id),
        client_ref=operation.client_ref,
        payload=dict(operation.payload),
        rationale=operation.rationale,
        confidence=operation.confidence,
        source_refs=[dict(item) for item in operation.source_refs],
        status=operation.status.value,
        result_entity_id=_uuid_str(operation.result_entity_id),
        error_metadata=dict(operation.error_metadata),
        created_at=operation.created_at,
        updated_at=operation.updated_at,
    )


def operation_from_model(row: GraphChangeOperationModel) -> GraphChangeOperation:
    return GraphChangeOperation(
        operation_id=UUID(row.operation_id),
        change_set_id=UUID(row.change_set_id),
        sequence=row.sequence,
        op=GraphChangeOp(row.op),
        entity_type=EntityType(row.entity_type),
        target_entity_id=_uuid(row.target_entity_id),
        client_ref=row.client_ref,
        payload=_dict(row.payload),
        rationale=row.rationale or "",
        confidence=row.confidence,
        source_refs=_list(row.source_refs),
        status=GraphChangeOperationStatus(row.status),
        result_entity_id=_uuid(row.result_entity_id),
        error_metadata=_dict(row.error_metadata),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def change_set_to_model(change_set: GraphChangeSet) -> GraphChangeSetModel:
    return GraphChangeSetModel(
        change_set_id=str(change_set.change_set_id),
        project_id=str(change_set.project_id),
        source_note_id=str(change_set.source_note_id),
        source_checksum=change_set.source_checksum,
        source_content_type=change_set.source_content_type,
        source_filename=change_set.source_filename,
        provider=change_set.provider,
        model=change_set.model,
        prompt_version=change_set.prompt_version,
        status=change_set.status.value,
        commit_message=change_set.commit_message,
        error_metadata=dict(change_set.error_metadata),
        created_by=change_set.created_by,
        created_at=change_set.created_at,
        updated_at=change_set.updated_at,
        committed_at=change_set.committed_at,
        committed_by=change_set.committed_by,
    )


def apply_change_set_to_model(row: GraphChangeSetModel, change_set: GraphChangeSet) -> None:
    row.project_id = str(change_set.project_id)
    row.source_note_id = str(change_set.source_note_id)
    row.source_checksum = change_set.source_checksum
    row.source_content_type = change_set.source_content_type
    row.source_filename = change_set.source_filename
    row.provider = change_set.provider
    row.model = change_set.model
    row.prompt_version = change_set.prompt_version
    row.status = change_set.status.value
    row.commit_message = change_set.commit_message
    row.error_metadata = dict(change_set.error_metadata)
    row.created_by = change_set.created_by
    row.created_at = change_set.created_at
    row.updated_at = change_set.updated_at
    row.committed_at = change_set.committed_at
    row.committed_by = change_set.committed_by


def change_set_from_model(
    row: GraphChangeSetModel,
    *,
    operations: Iterable[GraphChangeOperation] = (),
) -> GraphChangeSet:
    return GraphChangeSet(
        change_set_id=UUID(row.change_set_id),
        project_id=UUID(row.project_id),
        source_note_id=UUID(row.source_note_id),
        source_checksum=row.source_checksum,
        source_content_type=row.source_content_type,
        source_filename=row.source_filename,
        provider=row.provider,
        model=row.model,
        prompt_version=row.prompt_version,
        status=GraphChangeSetStatus(row.status),
        commit_message=row.commit_message,
        error_metadata=_dict(row.error_metadata),
        operations=list(operations),
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        committed_at=row.committed_at,
        committed_by=row.committed_by,
    )


class SQLAlchemyGraphChangeSetRepository(EntityRepository[GraphChangeSet]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def _operations_for(self, change_set_ids: list[str]) -> dict[str, list[GraphChangeOperation]]:
        if not change_set_ids:
            return {}
        rows = list(
            self._session.scalars(
                select(GraphChangeOperationModel)
                .where(GraphChangeOperationModel.change_set_id.in_(change_set_ids))
                .order_by(
                    GraphChangeOperationModel.change_set_id,
                    GraphChangeOperationModel.sequence,
                )
            )
        )
        operation_map: dict[str, list[GraphChangeOperation]] = {}
        for row in rows:
            operation_map.setdefault(row.change_set_id, []).append(operation_from_model(row))
        return operation_map

    def _from_rows(self, rows: list[GraphChangeSetModel]) -> list[GraphChangeSet]:
        operation_map = self._operations_for([row.change_set_id for row in rows])
        return [
            change_set_from_model(row, operations=operation_map.get(row.change_set_id, []))
            for row in rows
        ]

    def get(self, entity_id: UUID) -> GraphChangeSet | None:
        self._session.flush()
        row = self._session.get(GraphChangeSetModel, str(entity_id))
        if row is None:
            return None
        return self._from_rows([row])[0]

    def list(self) -> list[GraphChangeSet]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(GraphChangeSetModel).order_by(
                    GraphChangeSetModel.created_at.desc(),
                    GraphChangeSetModel.change_set_id,
                )
            )
        )
        return self._from_rows(rows)

    def save(self, entity: GraphChangeSet) -> None:
        entity_id = str(entity.change_set_id)
        row = self._session.get(GraphChangeSetModel, entity_id)
        if row is None:
            self._session.add(change_set_to_model(entity))
        else:
            apply_change_set_to_model(row, entity)
        replace_child_rows(
            self._session,
            GraphChangeOperationModel,
            GraphChangeOperationModel.change_set_id,
            entity_id,
            [operation_to_model(operation) for operation in entity.operations],
        )

    def delete(self, entity_id: UUID) -> GraphChangeSet | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(GraphChangeSetModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity

    def query(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        source_note_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[GraphChangeSet], int]:
        self._session.flush()
        stmt = select(GraphChangeSetModel)
        count_stmt = select(GraphChangeSetModel.change_set_id)
        if project_id is not None:
            stmt = stmt.where(GraphChangeSetModel.project_id == str(project_id))
            count_stmt = count_stmt.where(GraphChangeSetModel.project_id == str(project_id))
        if status is not None:
            stmt = stmt.where(GraphChangeSetModel.status == status)
            count_stmt = count_stmt.where(GraphChangeSetModel.status == status)
        if source_note_id is not None:
            stmt = stmt.where(GraphChangeSetModel.source_note_id == str(source_note_id))
            count_stmt = count_stmt.where(GraphChangeSetModel.source_note_id == str(source_note_id))
        stmt = stmt.order_by(
            GraphChangeSetModel.created_at.desc(),
            GraphChangeSetModel.change_set_id,
        )
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self._from_rows(rows), total
