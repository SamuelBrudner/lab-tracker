"""Graph draft SQLAlchemy repository."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import GraphChangeOperationModel, GraphChangeSetModel, UserModel
from lab_tracker.models import (
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftMode,
    GraphDraftSemanticType,
)
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mapper_parts.common import as_utc

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


def _as_utc_optional(value: Any) -> Any:
    return as_utc(value) if value is not None else None


def operation_to_model(operation: GraphChangeOperation) -> GraphChangeOperationModel:
    return GraphChangeOperationModel(
        operation_id=str(operation.operation_id),
        change_set_id=str(operation.change_set_id),
        sequence=operation.sequence,
        op=operation.op.value,
        entity_type=operation.entity_type.value,
        semantic_type=(
            operation.semantic_type.value if operation.semantic_type is not None else None
        ),
        target_entity_id=_uuid_str(operation.target_entity_id),
        client_ref=operation.client_ref,
        payload=dict(operation.payload),
        rationale=operation.rationale,
        confidence=operation.confidence,
        source_refs=[dict(item) for item in operation.source_refs],
        status=operation.status.value,
        review_note=operation.review_note,
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
        semantic_type=(
            GraphDraftSemanticType(row.semantic_type) if row.semantic_type else None
        ),
        target_entity_id=_uuid(row.target_entity_id),
        client_ref=row.client_ref,
        payload=_dict(row.payload),
        rationale=row.rationale or "",
        confidence=row.confidence,
        source_refs=_list(row.source_refs),
        status=GraphChangeOperationStatus(row.status),
        review_note=row.review_note,
        result_entity_id=_uuid(row.result_entity_id),
        error_metadata=_dict(row.error_metadata),
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
    )


def change_set_to_model(change_set: GraphChangeSet) -> GraphChangeSetModel:
    return GraphChangeSetModel(
        change_set_id=str(change_set.change_set_id),
        project_id=str(change_set.project_id),
        source_note_id=str(change_set.source_note_id),
        source_note_ids=[str(note_id) for note_id in change_set.source_note_ids],
        source_checksum=change_set.source_checksum,
        source_content_type=change_set.source_content_type,
        source_filename=change_set.source_filename,
        batch_key=change_set.batch_key,
        batch_window_start=change_set.batch_window_start,
        batch_window_end=change_set.batch_window_end,
        provider=change_set.provider,
        model=change_set.model,
        prompt_version=change_set.prompt_version,
        draft_mode=change_set.draft_mode.value,
        context_packet=dict(change_set.context_packet),
        summary=change_set.summary,
        uncertain_fields=list(change_set.uncertain_fields),
        clarification_requests=list(change_set.clarification_requests),
        status=change_set.status.value,
        commit_message=change_set.commit_message,
        error_metadata=dict(change_set.error_metadata),
        created_by=change_set.created_by,
        created_at=change_set.created_at,
        updated_at=change_set.updated_at,
        submitted_at=change_set.submitted_at,
        submitted_by=change_set.submitted_by,
        reviewed_at=change_set.reviewed_at,
        reviewed_by=change_set.reviewed_by,
        review_note=change_set.review_note,
        committed_at=change_set.committed_at,
        committed_by=change_set.committed_by,
    )


def apply_change_set_to_model(row: GraphChangeSetModel, change_set: GraphChangeSet) -> None:
    row.project_id = str(change_set.project_id)
    row.source_note_id = str(change_set.source_note_id)
    row.source_note_ids = [str(note_id) for note_id in change_set.source_note_ids]
    row.source_checksum = change_set.source_checksum
    row.source_content_type = change_set.source_content_type
    row.source_filename = change_set.source_filename
    row.batch_key = change_set.batch_key
    row.batch_window_start = change_set.batch_window_start
    row.batch_window_end = change_set.batch_window_end
    row.provider = change_set.provider
    row.model = change_set.model
    row.prompt_version = change_set.prompt_version
    row.draft_mode = change_set.draft_mode.value
    row.context_packet = dict(change_set.context_packet)
    row.summary = change_set.summary
    row.uncertain_fields = list(change_set.uncertain_fields)
    row.clarification_requests = list(change_set.clarification_requests)
    row.status = change_set.status.value
    row.commit_message = change_set.commit_message
    row.error_metadata = dict(change_set.error_metadata)
    row.created_by = change_set.created_by
    row.created_at = change_set.created_at
    row.updated_at = change_set.updated_at
    row.submitted_at = change_set.submitted_at
    row.submitted_by = change_set.submitted_by
    row.reviewed_at = change_set.reviewed_at
    row.reviewed_by = change_set.reviewed_by
    row.review_note = change_set.review_note
    row.committed_at = change_set.committed_at
    row.committed_by = change_set.committed_by


def change_set_from_model(
    row: GraphChangeSetModel,
    *,
    operations: Iterable[GraphChangeOperation] = (),
    usernames: dict[str, str] | None = None,
) -> GraphChangeSet:
    resolved_usernames = usernames or {}
    return GraphChangeSet(
        change_set_id=UUID(row.change_set_id),
        project_id=UUID(row.project_id),
        source_note_id=UUID(row.source_note_id),
        source_note_ids=[
            UUID(str(note_id)) for note_id in (row.source_note_ids or [row.source_note_id])
        ],
        source_checksum=row.source_checksum,
        source_content_type=row.source_content_type,
        source_filename=row.source_filename,
        batch_key=row.batch_key,
        batch_window_start=row.batch_window_start,
        batch_window_end=row.batch_window_end,
        provider=row.provider,
        model=row.model,
        prompt_version=row.prompt_version,
        draft_mode=GraphDraftMode(row.draft_mode or GraphDraftMode.GRAPH_CONTEXT.value),
        context_packet=_dict(row.context_packet),
        summary=row.summary or "",
        uncertain_fields=list(row.uncertain_fields or []),
        clarification_requests=list(row.clarification_requests or []),
        status=GraphChangeSetStatus(row.status),
        commit_message=row.commit_message,
        error_metadata=_dict(row.error_metadata),
        operations=list(operations),
        created_by=row.created_by,
        created_by_username=resolved_usernames.get(row.created_by or ""),
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
        submitted_at=_as_utc_optional(row.submitted_at),
        submitted_by=row.submitted_by,
        submitted_by_username=resolved_usernames.get(row.submitted_by or ""),
        reviewed_at=_as_utc_optional(row.reviewed_at),
        reviewed_by=row.reviewed_by,
        reviewed_by_username=resolved_usernames.get(row.reviewed_by or ""),
        review_note=row.review_note,
        committed_at=_as_utc_optional(row.committed_at),
        committed_by=row.committed_by,
        committed_by_username=resolved_usernames.get(row.committed_by or ""),
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
        user_ids = sorted(
            {
                user_id
                for row in rows
                for user_id in (
                    row.created_by,
                    row.submitted_by,
                    row.reviewed_by,
                    row.committed_by,
                )
                if user_id
            }
        )
        usernames: dict[str, str] = {}
        if user_ids:
            user_rows = list(
                self._session.scalars(select(UserModel).where(UserModel.user_id.in_(user_ids)))
            )
            usernames = {row.user_id: row.username for row in user_rows}
        return [
            change_set_from_model(
                row,
                operations=operation_map.get(row.change_set_id, []),
                usernames=usernames,
            )
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
        self._session.flush()
        row = self._session.get(GraphChangeSetModel, entity_id)
        if row is None:
            self._session.add(change_set_to_model(entity))
        else:
            apply_change_set_to_model(row, entity)
        self._session.flush()
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
        draft_mode: str | None = None,
        batch_key: str | None = None,
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
        if draft_mode is not None:
            stmt = stmt.where(GraphChangeSetModel.draft_mode == draft_mode)
            count_stmt = count_stmt.where(GraphChangeSetModel.draft_mode == draft_mode)
        if batch_key is not None:
            stmt = stmt.where(GraphChangeSetModel.batch_key == batch_key)
            count_stmt = count_stmt.where(GraphChangeSetModel.batch_key == batch_key)
        stmt = stmt.order_by(
            GraphChangeSetModel.created_at.desc(),
            GraphChangeSetModel.change_set_id,
        )
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self._from_rows(rows), total
