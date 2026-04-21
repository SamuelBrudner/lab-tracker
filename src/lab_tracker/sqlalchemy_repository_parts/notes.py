"""Note SQLAlchemy repository."""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import (
    NoteExtractedEntityModel,
    NoteModel,
    NoteTagSuggestionModel,
    NoteTargetModel,
)
from lab_tracker.models import Note
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mappers import (
    apply_note_to_model,
    entity_ref_from_model,
    extracted_entity_from_model,
    note_extracted_entity_models,
    note_from_model,
    note_tag_suggestion_models,
    note_target_models,
    note_to_model,
    tag_suggestion_from_model,
)

from .common import replace_child_rows


class SQLAlchemyNoteRepository(EntityRepository[Note]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def children_map(
        self,
        note_ids: list[str],
    ) -> tuple[dict[str, list[Any]], dict[str, list[Any]], dict[str, list[Any]]]:
        extracted_map: dict[str, list[Any]] = defaultdict(list)
        suggestion_map: dict[str, list[Any]] = defaultdict(list)
        target_map: dict[str, list[Any]] = defaultdict(list)
        if not note_ids:
            return extracted_map, suggestion_map, target_map
        extracted_rows = list(
            self._session.scalars(
                select(NoteExtractedEntityModel).where(NoteExtractedEntityModel.note_id.in_(note_ids))
            )
        )
        suggestion_rows = list(
            self._session.scalars(
                select(NoteTagSuggestionModel).where(NoteTagSuggestionModel.note_id.in_(note_ids))
            )
        )
        target_rows = list(
            self._session.scalars(select(NoteTargetModel).where(NoteTargetModel.note_id.in_(note_ids)))
        )
        for row in extracted_rows:
            extracted_map[row.note_id].append(row)
        for row in suggestion_rows:
            suggestion_map[row.note_id].append(row)
        for row in target_rows:
            target_map[row.note_id].append(row)
        return extracted_map, suggestion_map, target_map

    def notes_from_rows(self, rows: list[NoteModel]) -> list[Note]:
        note_ids = [row.note_id for row in rows]
        extracted_map, suggestion_map, target_map = self.children_map(note_ids)
        return [
            note_from_model(
                row,
                extracted_entities=[
                    extracted_entity_from_model(item)
                    for item in extracted_map.get(row.note_id, [])
                ],
                tag_suggestions=[
                    tag_suggestion_from_model(item)
                    for item in suggestion_map.get(row.note_id, [])
                ],
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
            NoteExtractedEntityModel,
            NoteExtractedEntityModel.note_id,
            entity_id,
            note_extracted_entity_models(entity),
        )
        replace_child_rows(
            self._session,
            NoteTagSuggestionModel,
            NoteTagSuggestionModel.note_id,
            entity_id,
            note_tag_suggestion_models(entity),
        )
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
