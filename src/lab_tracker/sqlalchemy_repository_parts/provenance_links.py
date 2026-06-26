"""SQLAlchemy repository for provenance links (content-hash lineage proposals)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import ProvenanceLinkModel
from lab_tracker.models import ProvenanceLink
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mappers import (
    apply_provenance_link_to_model,
    provenance_link_from_model,
    provenance_link_to_model,
)

from .common import apply_pagination, count_from_statement, uuid_values


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
