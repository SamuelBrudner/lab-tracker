"""Note SQLAlchemy repository."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import NoteModel, NoteTargetModel
from lab_tracker.models import Note
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mappers import (
    apply_note_to_model,
    entity_ref_from_model,
    note_from_model,
    note_target_models,
    note_to_model,
)

from .common import apply_pagination, count_from_statement, replace_child_rows, substring_pattern


class SQLAlchemyNoteRepository(EntityRepository[Note]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def target_map(self, note_ids: list[str]) -> dict[str, list[NoteTargetModel]]:
        target_map: dict[str, list[NoteTargetModel]] = {}
        if not note_ids:
            return target_map
        target_rows = list(
            self._session.scalars(select(NoteTargetModel).where(NoteTargetModel.note_id.in_(note_ids)))
        )
        for row in target_rows:
            target_map.setdefault(row.note_id, []).append(row)
        return target_map

    def notes_from_rows(self, rows: list[NoteModel]) -> list[Note]:
        note_ids = [row.note_id for row in rows]
        target_map = self.target_map(note_ids)
        return [
            note_from_model(
                row,
                targets=[entity_ref_from_model(item) for item in target_map.get(row.note_id, [])],
            )
            for row in rows
        ]

    def get(self, entity_id: UUID) -> Note | None:
        self._session.flush()
        row = self._session.get(NoteModel, str(entity_id))
        if row is None:
            return None
        return self.notes_from_rows([row])[0]

    def list(self) -> list[Note]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(NoteModel).order_by(NoteModel.created_at, NoteModel.note_id)
            )
        )
        return self.notes_from_rows(rows)

    def save(self, entity: Note) -> None:
        entity_id = str(entity.note_id)
        row = self._session.get(NoteModel, entity_id)
        if row is None:
            self._session.add(note_to_model(entity))
        else:
            apply_note_to_model(row, entity)
        replace_child_rows(
            self._session,
            NoteTargetModel,
            NoteTargetModel.note_id,
            entity_id,
            note_target_models(entity),
        )

    def delete(self, entity_id: UUID) -> Note | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(NoteModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity

    def query(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        search: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Note], int]:
        self._session.flush()
        stmt = select(NoteModel)
        count_stmt = select(NoteModel.note_id)
        if project_id is not None:
            stmt = stmt.where(NoteModel.project_id == str(project_id))
            count_stmt = count_stmt.where(NoteModel.project_id == str(project_id))
        if status is not None:
            stmt = stmt.where(NoteModel.status == status)
            count_stmt = count_stmt.where(NoteModel.status == status)
        pattern = substring_pattern(search)
        if pattern is not None:
            search_clause = or_(
                NoteModel.raw_content.ilike(pattern, escape="\\"),
                NoteModel.transcribed_text.ilike(pattern, escape="\\"),
            )
            stmt = stmt.where(search_clause)
            count_stmt = count_stmt.where(search_clause)
        if target_entity_type is not None and target_entity_id is not None:
            stmt = stmt.join(NoteTargetModel, NoteTargetModel.note_id == NoteModel.note_id).where(
                NoteTargetModel.entity_type == target_entity_type,
                NoteTargetModel.entity_id == str(target_entity_id),
            )
            count_stmt = count_stmt.join(
                NoteTargetModel,
                NoteTargetModel.note_id == NoteModel.note_id,
            ).where(
                NoteTargetModel.entity_type == target_entity_type,
                NoteTargetModel.entity_id == str(target_entity_id),
            )
        stmt = stmt.order_by(NoteModel.created_at, NoteModel.note_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self.notes_from_rows(rows), total
