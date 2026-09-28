"""SQLAlchemy repository for provenance links (content-hash, exact-id, time-window)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import String, cast, func, literal, or_, select, union_all
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import (
    DatasetFileModel,
    DatasetModel,
    NoteModel,
    NoteTargetModel,
    ProvenanceLinkModel,
)
from lab_tracker.models import (
    MIN_CARRIERS_PER_HASH,
    ContentHashCarrier,
    EntityRef,
    EntityType,
    Note,
    NoteStatus,
    ProvenanceLink,
)
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mapper_parts.common import as_utc
from lab_tracker.sqlalchemy_mappers import (
    apply_provenance_link_to_model,
    provenance_link_from_model,
    provenance_link_to_model,
)

from .common import apply_pagination, count_from_statement, uuid_values
from .notes import SQLAlchemyNoteRepository


class SQLAlchemyProvenanceLinkRepository(EntityRepository[ProvenanceLink]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def get(self, entity_id: UUID) -> ProvenanceLink | None:
        self._session.flush()
        row = self._session.get(ProvenanceLinkModel, str(entity_id))
        if row is None:
            return None
        return provenance_link_from_model(row)

    def list(self) -> list[ProvenanceLink]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(ProvenanceLinkModel).order_by(
                    ProvenanceLinkModel.created_at,
                    ProvenanceLinkModel.link_id,
                )
            )
        )
        return [provenance_link_from_model(row) for row in rows]

    def save(self, entity: ProvenanceLink) -> None:
        entity_id = str(entity.link_id)
        row = self._session.get(ProvenanceLinkModel, entity_id)
        if row is None:
            self._session.add(provenance_link_to_model(entity))
        else:
            apply_provenance_link_to_model(row, entity)
        self._session.flush()

    def delete(self, entity_id: UUID) -> ProvenanceLink | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(ProvenanceLinkModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity

    def list_by_project(
        self,
        project_id: UUID,
        *,
        status: str | None = None,
    ) -> list[ProvenanceLink]:
        self._session.flush()
        stmt = select(ProvenanceLinkModel).where(
            ProvenanceLinkModel.project_id == str(project_id)
        )
        if status is not None:
            stmt = stmt.where(ProvenanceLinkModel.status == status)
        stmt = stmt.order_by(ProvenanceLinkModel.created_at, ProvenanceLinkModel.link_id)
        return [provenance_link_from_model(row) for row in self._session.scalars(stmt)]

    def list_content_hash_carriers(self, project_id: UUID) -> list[ContentHashCarrier]:
        """One UNION over indexed note hashes and uploaded dataset-file checksums.

        Only hashes carried by at least ``MIN_CARRIERS_PER_HASH`` rows in the
        project come back, ordered so the earliest capture of each hash leads.
        """

        self._session.flush()
        project_value = str(project_id)
        note_rows = select(
            NoteModel.evidence_content_hash.label("content_hash"),
            literal(EntityType.NOTE.value).label("entity_type"),
            cast(NoteModel.note_id, String()).label("entity_id"),
            NoteModel.created_at.label("captured_at"),
        ).where(
            NoteModel.project_id == project_value,
            NoteModel.evidence_content_hash.is_not(None),
        )
        file_rows = (
            select(
                DatasetFileModel.checksum.label("content_hash"),
                literal(EntityType.DATASET.value).label("entity_type"),
                cast(DatasetModel.dataset_id, String()).label("entity_id"),
                DatasetFileModel.created_at.label("captured_at"),
            )
            .select_from(DatasetFileModel)
            .join(DatasetModel, DatasetModel.dataset_id == DatasetFileModel.dataset_id)
            .where(DatasetModel.project_id == project_value)
        )
        carriers = union_all(note_rows, file_rows).subquery("content_hash_carriers")
        shared = (
            select(carriers.c.content_hash)
            .group_by(carriers.c.content_hash)
            .having(func.count() >= MIN_CARRIERS_PER_HASH)
        )
        stmt = (
            select(carriers)
            .where(carriers.c.content_hash.in_(shared))
            .order_by(
                carriers.c.content_hash,
                carriers.c.captured_at,
                carriers.c.entity_type,
                carriers.c.entity_id,
            )
        )
        return [
            ContentHashCarrier(
                content_hash=row.content_hash,
                entity=EntityRef(
                    entity_type=EntityType(row.entity_type),
                    entity_id=UUID(row.entity_id),
                ),
                captured_at=as_utc(row.captured_at),
            )
            for row in self._session.execute(stmt)
        ]

    def list_identifier_carriers(self, project_id: UUID, keys: Sequence[str]) -> list[Note]:
        """Notes whose metadata sets any of ``keys``; the JSON filter runs in SQL.

        Bounded by the captures that actually name an identifier, so a project
        full of typed notes costs the detector nothing.
        """

        if not keys:
            return []
        self._session.flush()
        stmt = (
            select(NoteModel)
            .where(
                NoteModel.project_id == str(project_id),
                or_(*(NoteModel.note_metadata[key].as_string().is_not(None) for key in keys)),
            )
            .order_by(NoteModel.created_at, NoteModel.note_id)
        )
        rows = list(self._session.scalars(stmt))
        return SQLAlchemyNoteRepository(self._session).notes_from_rows(rows)

    def list_time_window_candidates(
        self,
        project_id: UUID,
        *,
        created_since: datetime,
        excluded_metadata_keys: Sequence[str],
        origins: Sequence[str],
    ) -> list[Note]:
        """Recent unarchived captures that name no session; every filter runs in SQL.

        Bounded by ``created_since`` (the detector's lookback), so a long-lived
        project costs one recent-window scan, not a full-history one.
        """

        self._session.flush()
        session_targeted = select(NoteTargetModel.note_id).where(
            NoteTargetModel.entity_type == EntityType.SESSION.value
        )
        stmt = (
            select(NoteModel)
            .where(
                NoteModel.project_id == str(project_id),
                NoteModel.created_at >= created_since,
                NoteModel.status != NoteStatus.ARCHIVED.value,
                NoteModel.origin.in_(list(origins)),
                NoteModel.note_id.not_in(session_targeted),
                *(
                    NoteModel.note_metadata[key].as_string().is_(None)
                    for key in excluded_metadata_keys
                ),
            )
            .order_by(NoteModel.created_at, NoteModel.note_id)
        )
        rows = list(self._session.scalars(stmt))
        return SQLAlchemyNoteRepository(self._session).notes_from_rows(rows)

    def query(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[ProvenanceLink], int]:
        self._session.flush()
        if project_ids is not None and not project_ids:
            return [], 0
        stmt = select(ProvenanceLinkModel)
        count_stmt = select(ProvenanceLinkModel.link_id)
        if project_id is not None:
            stmt = stmt.where(ProvenanceLinkModel.project_id == str(project_id))
            count_stmt = count_stmt.where(ProvenanceLinkModel.project_id == str(project_id))
        if project_ids is not None:
            project_values = uuid_values(project_ids)
            stmt = stmt.where(ProvenanceLinkModel.project_id.in_(project_values))
            count_stmt = count_stmt.where(ProvenanceLinkModel.project_id.in_(project_values))
        if status is not None:
            stmt = stmt.where(ProvenanceLinkModel.status == status)
            count_stmt = count_stmt.where(ProvenanceLinkModel.status == status)
        if recent_first:
            stmt = stmt.order_by(
                ProvenanceLinkModel.created_at.desc(),
                ProvenanceLinkModel.link_id.desc(),
            )
        else:
            stmt = stmt.order_by(
                ProvenanceLinkModel.created_at,
                ProvenanceLinkModel.link_id,
            )
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [provenance_link_from_model(row) for row in rows], total
