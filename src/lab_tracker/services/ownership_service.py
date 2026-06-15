"""Ownership reassignment service."""

from __future__ import annotations

from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, Role, require_role
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import OwnershipReassignment, utc_now
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.shared import actor_user_fk, actor_user_id

ADMIN_ROLES = {Role.ADMIN}


class OwnershipReassignmentService(BaseService):
    def __init__(self, context: ServiceContext) -> None:
        super().__init__(context)

    def reassign_ownership(
        self,
        *,
        from_user_id: UUID,
        to_user_id: UUID,
        reason: str | None = None,
        actor: AuthContext | None = None,
    ) -> OwnershipReassignment:
        require_role(actor, ADMIN_ROLES)
        if from_user_id == to_user_id:
            raise ValidationError("from_user_id and to_user_id must differ.")
        cleaned_reason = (reason or "").strip()
        with self.unit_of_work() as repository:
            if not repository.user_exists(from_user_id):
                raise NotFoundError("Departing user does not exist.")
            if not repository.user_exists(to_user_id):
                raise NotFoundError("Successor user does not exist.")
            return repository.reassign_ownership(
                reassignment_id=uuid4(),
                from_user_id=from_user_id,
                to_user_id=to_user_id,
                reason=cleaned_reason,
                created_by=actor_user_id(actor),
                created_by_user_id=actor_user_fk(actor, repository),
                created_at=utc_now(),
            )

    def get_ownership_reassignment(
        self,
        reassignment_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> OwnershipReassignment:
        require_role(actor, ADMIN_ROLES)
        return self.get_from_repository(
            entity_id=reassignment_id,
            label="Ownership reassignment",
            loader=lambda repository: repository.ownership_reassignments.get(
                reassignment_id
            ),
        )

    def list_ownership_reassignments(
        self,
        *,
        from_user_id: UUID | None = None,
        to_user_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
        actor: AuthContext | None = None,
    ) -> tuple[list[OwnershipReassignment], int]:
        require_role(actor, ADMIN_ROLES)
        return self.repository.query_ownership_reassignments(
            from_user_id=from_user_id,
            to_user_id=to_user_id,
            limit=limit,
            offset=offset,
        )
