"""Supervision relationship service."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.errors import (
    ConflictError,
    NotFoundError,
    OpaqueTargetNotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from lab_tracker.models import ProjectMembershipRole, SupervisionEdge, utc_now
from lab_tracker.patching import NOT_PROVIDED, PatchValue, is_provided
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.shared import WRITE_ROLES

_EDGE_NOT_FOUND_MESSAGE = "Supervision edge does not exist."
_MANAGE_DENIED_MESSAGE = (
    "Supervision edges can only be managed by an admin or by an owner of a "
    "project group containing both users."
)


class SupervisionService(BaseService):
    """Dated supervision edges, which feed ``actedOnBehalfOf`` in provenance.

    Authority (global role ``editor`` or ``admin`` is required throughout):

    - Manage (create, update, delete): a global admin, or an owner of a project
      group whose members include both the supervisor and the supervisee. Being
      one of the two users is not enough, so nobody can assert their own
      supervisor or claim to supervise someone else.
    - Read (get, list): anyone who may manage the edge, plus its supervisor and
      supervisee. An edge outside the caller's read scope is an opaque ``404``;
      a readable edge the caller may not manage is an explicit ``403``.
    """

    def __init__(
        self,
        context: ServiceContext,
        *,
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.authorization = authorization

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
        self._require_manage_pair(
            actor,
            supervisor_user_id=supervisor_user_id,
            supervisee_user_id=supervisee_user_id,
        )
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
        edge: SupervisionEdge = self.get_from_repository(
            entity_id=edge_id,
            label="Supervision edge",
            loader=lambda repository: repository.supervision_edges.get(edge_id),
        )
        if not self._can_read_edge(actor, edge, self._owned_group_members(actor)):
            raise OpaqueTargetNotFoundError(_EDGE_NOT_FOUND_MESSAGE)
        return edge

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
        if self.authorization.has_global_admin(actor):
            return self.repository.query_supervision_edges(
                supervisor_user_id=supervisor_user_id,
                supervisee_user_id=supervisee_user_id,
                active_only=active_only,
                as_of=as_of,
                limit=limit,
                offset=offset,
            )
        # Scope before paginating so hidden edges never consume a page. The
        # edge set is people-scale, so filtering the matching rows in memory is
        # bounded by the lab's supervision history.
        owned_group_members = self._owned_group_members(actor)
        candidates, _ = self.repository.query_supervision_edges(
            supervisor_user_id=supervisor_user_id,
            supervisee_user_id=supervisee_user_id,
            active_only=active_only,
            as_of=as_of,
            limit=None,
            offset=0,
        )
        visible = [
            edge for edge in candidates if self._can_read_edge(actor, edge, owned_group_members)
        ]
        end = None if limit is None else offset + limit
        return visible[offset:end], len(visible)

    def update_supervision_edge(
        self,
        edge_id: UUID,
        *,
        supervisor_user_id: PatchValue[UUID | None] = NOT_PROVIDED,
        supervisee_user_id: PatchValue[UUID | None] = NOT_PROVIDED,
        started_at: PatchValue[datetime | None] = NOT_PROVIDED,
        ended_at: PatchValue[datetime | None] = NOT_PROVIDED,
        actor: AuthContext | None = None,
    ) -> SupervisionEdge:
        require_role(actor, WRITE_ROLES)
        edge = self.get_supervision_edge(edge_id, actor=actor)
        self._require_manage_pair(
            actor,
            supervisor_user_id=edge.supervisor_user_id,
            supervisee_user_id=edge.supervisee_user_id,
        )
        before = edge.model_copy(deep=True)
        if is_provided(supervisor_user_id):
            if supervisor_user_id is None:
                raise ValidationError("supervisor_user_id must not be null.")
            edge.supervisor_user_id = supervisor_user_id
        if is_provided(supervisee_user_id):
            if supervisee_user_id is None:
                raise ValidationError("supervisee_user_id must not be null.")
            edge.supervisee_user_id = supervisee_user_id
        if is_provided(started_at):
            if started_at is None:
                raise ValidationError("started_at must not be null.")
            edge.started_at = started_at
        if is_provided(ended_at):
            edge.ended_at = ended_at
        # Retargeting must stay within the actor's authority too.
        self._require_manage_pair(
            actor,
            supervisor_user_id=edge.supervisor_user_id,
            supervisee_user_id=edge.supervisee_user_id,
        )
        self._validate_edge(edge)
        if edge.ended_at is None:
            self._ensure_active_pair_available(edge, excluding_edge_id=edge.edge_id)
        if edge == before:
            return edge
        edge.updated_at = utc_now()
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
        self._require_manage_pair(
            actor,
            supervisor_user_id=edge.supervisor_user_id,
            supervisee_user_id=edge.supervisee_user_id,
        )
        with self.unit_of_work() as repository:
            repository.supervision_edges.delete(edge_id)
        return edge

    def _owned_group_members(self, actor: AuthContext | None) -> list[set[UUID]]:
        """Member-id sets of every project group the actor owns (empty for admins)."""

        if actor is None or self.authorization.has_global_admin(actor):
            return []
        memberships, _ = self.repository.query_group_memberships(
            user_id=actor.user_id,
            limit=None,
            offset=0,
        )
        owned_sets: list[set[UUID]] = []
        for membership in memberships:
            if membership.role != ProjectMembershipRole.OWNER:
                continue
            members, _ = self.repository.query_group_memberships(
                group_id=membership.group_id,
                limit=None,
                offset=0,
            )
            owned_sets.append({member.user_id for member in members})
        return owned_sets

    def _can_manage_pair(
        self,
        actor: AuthContext | None,
        owned_group_members: list[set[UUID]],
        *,
        supervisor_user_id: UUID,
        supervisee_user_id: UUID,
    ) -> bool:
        if self.authorization.has_global_admin(actor):
            return True
        return any(
            supervisor_user_id in members and supervisee_user_id in members
            for members in owned_group_members
        )

    def _can_read_edge(
        self,
        actor: AuthContext | None,
        edge: SupervisionEdge,
        owned_group_members: list[set[UUID]],
    ) -> bool:
        if actor is not None and actor.user_id in {
            edge.supervisor_user_id,
            edge.supervisee_user_id,
        }:
            return True
        return self._can_manage_pair(
            actor,
            owned_group_members,
            supervisor_user_id=edge.supervisor_user_id,
            supervisee_user_id=edge.supervisee_user_id,
        )

    def _require_manage_pair(
        self,
        actor: AuthContext | None,
        *,
        supervisor_user_id: UUID,
        supervisee_user_id: UUID,
    ) -> None:
        if not self._can_manage_pair(
            actor,
            self._owned_group_members(actor),
            supervisor_user_id=supervisor_user_id,
            supervisee_user_id=supervisee_user_id,
        ):
            raise PermissionDeniedError(_MANAGE_DENIED_MESSAGE)

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
