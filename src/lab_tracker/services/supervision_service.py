"""Supervision relationship service."""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.errors import ConflictError, NotFoundError, ValidationError
from lab_tracker.models import SupervisionEdge, utc_now
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.shared import WRITE_ROLES

_UNSET: Final = object()


class SupervisionService(BaseService):
    def __init__(self, context: ServiceContext) -> None:
        super().__init__(context)

    def create_supervision_edge(
        self,
        *,
        supervisor_user_id: UUID,
        supervisee_user_id: UUID,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        actor: AuthContext | None = None,
    ) -> SupervisionEdge:
        require_role(actor, WRITE_ROLES)
        now = utc_now()
        edge = SupervisionEdge(
            edge_id=uuid4(),
            supervisor_user_id=supervisor_user_id,
            supervisee_user_id=supervisee_user_id,
            started_at=started_at or now,
            ended_at=ended_at,
            created_at=now,
            updated_at=now,
        )
        self._validate_edge(edge)
        if edge.ended_at is None:
            self._ensure_active_pair_available(edge)
        with self.unit_of_work() as repository:
            repository.supervision_edges.save(edge)
            saved = repository.supervision_edges.get(edge.edge_id)
        return saved or edge

    def get_supervision_edge(
        self,
        edge_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> SupervisionEdge:
        require_role(actor, WRITE_ROLES)
        return self.get_from_repository(
            entity_id=edge_id,
            label="Supervision edge",
            loader=lambda repository: repository.supervision_edges.get(edge_id),
        )

    def list_supervision_edges(
        self,
        *,
        supervisor_user_id: UUID | None = None,
        supervisee_user_id: UUID | None = None,
        active_only: bool = False,
        as_of: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        actor: AuthContext | None = None,
    ) -> tuple[list[SupervisionEdge], int]:
        require_role(actor, WRITE_ROLES)
        return self.repository.query_supervision_edges(
            supervisor_user_id=supervisor_user_id,
            supervisee_user_id=supervisee_user_id,
            active_only=active_only,
            as_of=as_of,
            limit=limit,
            offset=offset,
        )

    def update_supervision_edge(
        self,
        edge_id: UUID,
        *,
        supervisor_user_id: UUID | None | object = _UNSET,
        supervisee_user_id: UUID | None | object = _UNSET,
        started_at: datetime | None | object = _UNSET,
        ended_at: datetime | None | object = _UNSET,
        actor: AuthContext | None = None,
    ) -> SupervisionEdge:
        require_role(actor, WRITE_ROLES)
        edge = self.get_supervision_edge(edge_id, actor=actor)
        if supervisor_user_id is not _UNSET:
            if supervisor_user_id is None:
                raise ValidationError("supervisor_user_id must not be null.")
            edge.supervisor_user_id = supervisor_user_id
        if supervisee_user_id is not _UNSET:
            if supervisee_user_id is None:
                raise ValidationError("supervisee_user_id must not be null.")
            edge.supervisee_user_id = supervisee_user_id
        if started_at is not _UNSET:
            if started_at is None:
                raise ValidationError("started_at must not be null.")
            edge.started_at = started_at
        if ended_at is not _UNSET:
            edge.ended_at = ended_at
        edge.updated_at = utc_now()
        self._validate_edge(edge)
        if edge.ended_at is None:
            self._ensure_active_pair_available(edge, excluding_edge_id=edge.edge_id)
        with self.unit_of_work() as repository:
            repository.supervision_edges.save(edge)
            saved = repository.supervision_edges.get(edge.edge_id)
        return saved or edge

    def delete_supervision_edge(
        self,
        edge_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> SupervisionEdge:
        require_role(actor, WRITE_ROLES)
        edge = self.get_supervision_edge(edge_id, actor=actor)
        with self.unit_of_work() as repository:
            repository.supervision_edges.delete(edge_id)
        return edge

    def _validate_edge(self, edge: SupervisionEdge) -> None:
        if edge.supervisor_user_id == edge.supervisee_user_id:
            raise ValidationError("supervisor_user_id and supervisee_user_id must differ.")
        if not self.repository.user_exists(edge.supervisor_user_id):
            raise NotFoundError("Supervisor user does not exist.")
        if not self.repository.user_exists(edge.supervisee_user_id):
            raise NotFoundError("Supervisee user does not exist.")
        if edge.ended_at is not None and edge.ended_at <= edge.started_at:
            raise ValidationError("ended_at must be after started_at.")

    def _ensure_active_pair_available(
        self,
        edge: SupervisionEdge,
        *,
        excluding_edge_id: UUID | None = None,
    ) -> None:
        existing_edges, _ = self.repository.query_supervision_edges(
            supervisor_user_id=edge.supervisor_user_id,
            supervisee_user_id=edge.supervisee_user_id,
            active_only=True,
            limit=None,
            offset=0,
        )
        for existing in existing_edges:
            if existing.edge_id != excluding_edge_id:
                raise ConflictError("An active supervision edge already exists for this pair.")
