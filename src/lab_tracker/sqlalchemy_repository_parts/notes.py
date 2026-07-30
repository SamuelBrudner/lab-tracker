"""Note SQLAlchemy repository."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import NoteModel, NoteTargetModel
from lab_tracker.models import Note, NoteMetadataScalar
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mappers import (
    apply_note_to_model,
    entity_ref_from_model,
    note_from_model,
    note_target_models,
    note_to_model,
)

from .common import (
    apply_pagination,
    count_from_statement,
    replace_child_rows,
    substring_pattern,
    uuid_values,
)

_AUTO_TRANSCRIPTION_CLAIM_ID = "auto_transcription_claim_id"
_AUTO_TRANSCRIPTION_CLAIMED_AT = "auto_transcription_claimed_at"


def _metadata_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


class SQLAlchemyNoteRepository(EntityRepository[Note]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def target_map(self, note_ids: list[str]) -> dict[str, list[NoteTargetModel]]:
        target_map: dict[str, list[NoteTargetModel]] = {}
        if not note_ids:
            return target_map
        target_rows = list(
            self._session.scalars(
                select(NoteTargetModel).where(NoteTargetModel.note_id.in_(note_ids))
            )
        )
        for row in target_rows:
            target_map.setdefault(str(row.note_id), []).append(row)
        return target_map

    def notes_from_rows(self, rows: list[NoteModel]) -> list[Note]:
        note_ids = [row.note_id for row in rows]
        target_map = self.target_map(note_ids)
        return [
            note_from_model(
                row,
                targets=[
                    entity_ref_from_model(item) for item in target_map.get(str(row.note_id), [])
                ],
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
        self._session.flush()
        replace_child_rows(
            self._session,
            NoteTargetModel,
            NoteTargetModel.note_id,
            entity_id,
            note_target_models(entity),
        )

    def try_claim_auto_transcription(
        self,
        note_id: UUID,
        *,
        claim_id: UUID,
        claimed_at: datetime,
        stale_before: datetime,
    ) -> Note | None:
        """Claim an automatic provider call without holding a long transaction."""

        self._session.flush()
        row = self._session.scalar(
            select(NoteModel)
            .where(NoteModel.note_id == str(note_id))
            .execution_options(populate_existing=True)
        )
        if row is None or (row.transcribed_text or "").strip():
            return None

        metadata = dict(row.note_metadata or {})
        existing_claim = metadata.get(_AUTO_TRANSCRIPTION_CLAIM_ID)
        existing_claimed_at = _metadata_datetime(
            metadata.get(_AUTO_TRANSCRIPTION_CLAIMED_AT)
        )
        if existing_claim is not None and (
            existing_claimed_at is None or existing_claimed_at > stale_before
        ):
            return None

        prior_updated_at = row.updated_at
        metadata[_AUTO_TRANSCRIPTION_CLAIM_ID] = str(claim_id)
        metadata[_AUTO_TRANSCRIPTION_CLAIMED_AT] = claimed_at.isoformat()
        result = self._session.execute(
            update(NoteModel)
            .where(
                NoteModel.note_id == str(note_id),
                NoteModel.updated_at == prior_updated_at,
                or_(
                    NoteModel.transcribed_text.is_(None),
                    NoteModel.transcribed_text == "",
                ),
            )
            .values(
                note_metadata=metadata,
                updated_at=claimed_at,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self._session.expire_all()
            return None
        self._session.flush()
        self._session.expire_all()
        return self.get(note_id)

    def apply_auto_transcription_result(
        self,
        note_id: UUID,
        *,
        claim_id: UUID,
        claimed_updated_at: datetime,
        text: str,
        metadata_updates: dict[str, NoteMetadataScalar],
        updated_at: datetime,
    ) -> Note | None:
        """Apply one claimed result and discard it after any intervening edit."""

        self._session.flush()
        row = self._session.scalars(
            select(NoteModel)
            .where(NoteModel.note_id == str(note_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if row is None:
            return None
        metadata = dict(row.note_metadata or {})
        if metadata.get(_AUTO_TRANSCRIPTION_CLAIM_ID) != str(claim_id):
            return self.notes_from_rows([row])[0]

        metadata.pop(_AUTO_TRANSCRIPTION_CLAIM_ID, None)
        metadata.pop(_AUTO_TRANSCRIPTION_CLAIMED_AT, None)
        if (
            row.updated_at != claimed_updated_at
            or bool((row.transcribed_text or "").strip())
        ):
            row.note_metadata = metadata
            row.updated_at = updated_at
            self._session.flush()
            return self.notes_from_rows([row])[0]

        metadata.update(metadata_updates)
        row.transcribed_text = text
        row.note_metadata = metadata
        row.updated_at = updated_at
        self._session.flush()
        return self.notes_from_rows([row])[0]

    def release_auto_transcription_claim(
        self,
        note_id: UUID,
        *,
        claim_id: UUID,
        updated_at: datetime,
    ) -> Note | None:
        """Remove only the caller's claim, preserving later human mutations."""

        self._session.flush()
        row = self._session.scalars(
            select(NoteModel)
            .where(NoteModel.note_id == str(note_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if row is None:
            return None
        metadata = dict(row.note_metadata or {})
        if metadata.get(_AUTO_TRANSCRIPTION_CLAIM_ID) != str(claim_id):
            return self.notes_from_rows([row])[0]
        metadata.pop(_AUTO_TRANSCRIPTION_CLAIM_ID, None)
        metadata.pop(_AUTO_TRANSCRIPTION_CLAIMED_AT, None)
        row.note_metadata = metadata
        row.updated_at = updated_at
        self._session.flush()
        return self.notes_from_rows([row])[0]

    def apply_transcription_result(
        self,
        note_id: UUID,
        *,
        text: str,
        metadata_updates: dict[str, NoteMetadataScalar],
        updated_at: datetime,
        expected_updated_at: datetime | None = None,
        only_if_unchanged: bool = False,
    ) -> Note | None:
        """Merge a provider transcript without restoring a stale note snapshot."""

        self._session.flush()
        row = self._session.scalars(
            select(NoteModel)
            .where(NoteModel.note_id == str(note_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if row is None:
            return None

        if only_if_unchanged and (
            bool((row.transcribed_text or "").strip())
            or (
                expected_updated_at is not None
                and row.updated_at != expected_updated_at
            )
        ):
            return self.notes_from_rows([row])[0]

        metadata = dict(row.note_metadata or {})
        metadata.update(metadata_updates)
        row.transcribed_text = text
        row.note_metadata = metadata
        row.updated_at = updated_at
        self._session.flush()
        return self.notes_from_rows([row])[0]

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
        project_ids: set[UUID] | None = None,
        note_ids: set[UUID] | None = None,
        status: str | None = None,
        search: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        client_capture_id: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Note], int]:
        self._session.flush()
        if project_ids is not None and not project_ids:
            return [], 0
        if note_ids is not None and not note_ids:
            return [], 0
        stmt = select(NoteModel)
        count_stmt = select(NoteModel.note_id)
        if project_id is not None:
            stmt = stmt.where(NoteModel.project_id == str(project_id))
            count_stmt = count_stmt.where(NoteModel.project_id == str(project_id))
        if project_ids is not None:
            project_values = uuid_values(project_ids)
            stmt = stmt.where(NoteModel.project_id.in_(project_values))
            count_stmt = count_stmt.where(NoteModel.project_id.in_(project_values))
        if note_ids is not None:
            note_values = uuid_values(note_ids)
            stmt = stmt.where(NoteModel.note_id.in_(note_values))
            count_stmt = count_stmt.where(NoteModel.note_id.in_(note_values))
        if status is not None:
            stmt = stmt.where(NoteModel.status == status)
            count_stmt = count_stmt.where(NoteModel.status == status)
        if created_by is not None:
            stmt = stmt.where(NoteModel.created_by_user_id == created_by)
            count_stmt = count_stmt.where(NoteModel.created_by_user_id == created_by)
        if since is not None:
            stmt = stmt.where(NoteModel.created_at >= since)
            count_stmt = count_stmt.where(NoteModel.created_at >= since)
        if until is not None:
            stmt = stmt.where(NoteModel.created_at < until)
            count_stmt = count_stmt.where(NoteModel.created_at < until)
        if client_capture_id is not None:
            stmt = stmt.where(NoteModel.client_capture_id == client_capture_id)
            count_stmt = count_stmt.where(NoteModel.client_capture_id == client_capture_id)
        pattern = substring_pattern(search)
        if pattern is not None:
            search_clause = or_(
                NoteModel.raw_content.ilike(pattern, escape="\\"),
                NoteModel.transcribed_text.ilike(pattern, escape="\\"),
            )
            stmt = stmt.where(search_clause)
            count_stmt = count_stmt.where(search_clause)
        if target_entity_type is not None or target_entity_id is not None:
            matching_note_ids = select(NoteTargetModel.note_id)
            if target_entity_type is not None:
                matching_note_ids = matching_note_ids.where(
                    NoteTargetModel.entity_type == target_entity_type
                )
            if target_entity_id is not None:
                matching_note_ids = matching_note_ids.where(
                    NoteTargetModel.entity_id == str(target_entity_id)
                )
            stmt = stmt.where(NoteModel.note_id.in_(matching_note_ids))
            count_stmt = count_stmt.where(NoteModel.note_id.in_(matching_note_ids))
        if recent_first:
            stmt = stmt.order_by(NoteModel.created_at.desc(), NoteModel.note_id.desc())
        else:
            stmt = stmt.order_by(NoteModel.created_at, NoteModel.note_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self.notes_from_rows(rows), total
