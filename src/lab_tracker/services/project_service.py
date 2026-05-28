"""Project domain service."""

from __future__ import annotations

from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import (
    Project,
    ProjectMembership,
    ProjectMembershipRole,
    ProjectStatus,
    utc_now,
)
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.shared import (
    WRITE_ROLES,
    actor_user_id,
    ensure_non_empty,
)


class ProjectService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.authorization = authorization

    def create_project(
        self,
        name: str,
        description: str = "",
        status: ProjectStatus = ProjectStatus.ACTIVE,
        *,
        actor: AuthContext | None = None,
    ) -> Project:
        require_role(actor, WRITE_ROLES)
        ensure_non_empty(name, "name")
        project = Project(
            project_id=uuid4(),
            name=name.strip(),
            description=description.strip(),
            status=status,
            created_by=actor_user_id(actor),
        )
        with self.unit_of_work() as repository:
            repository.projects.save(project)
        if actor is not None and not self.authorization.has_global_admin(actor):
            membership = ProjectMembership(
                membership_id=uuid4(),
                project_id=project.project_id,
                user_id=actor.user_id,
                role=ProjectMembershipRole.OWNER,
                created_by=actor_user_id(actor),
            )
            with self.unit_of_work() as repository:
                repository.project_memberships.save(membership)
        return project

    def get_project(self, project_id: UUID) -> Project:
        return self.get_from_repository(
            entity_id=project_id,
            label="Project",
            loader=lambda repository: repository.projects.get(project_id),
        )

    def list_projects(self) -> list[Project]:
        return self.list_from_repository(
            loader=lambda repository: repository.projects.list(),
        )

    def update_project(
        self,
        project_id: UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        status: ProjectStatus | None = None,
        actor: AuthContext | None = None,
    ) -> Project:
        require_role(actor, WRITE_ROLES)
        project = self.get_project(project_id)
        if name is not None:
            ensure_non_empty(name, "name")
            project.name = name.strip()
        if description is not None:
            project.description = description.strip()
        if status is not None:
            project.status = status
        project.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.projects.save(project)
        return project

    def delete_project(self, project_id: UUID, *, actor: AuthContext | None = None) -> Project:
        require_role(actor, WRITE_ROLES)
        project = self.get_project(project_id)
        with self.unit_of_work() as repository:
            repository.projects.delete(project_id)
        return project

    def get_project_membership(self, membership_id: UUID) -> ProjectMembership:
        return self.get_from_repository(
            entity_id=membership_id,
            label="Project membership",
            loader=lambda repository: repository.project_memberships.get(membership_id),
        )

    def get_project_membership_for_user(
        self,
        project_id: UUID,
        user_id: UUID,
    ) -> ProjectMembership | None:
        return self.repository.get_project_membership(
            project_id=project_id,
            user_id=user_id,
        )

    def list_project_memberships(
        self,
        *,
        project_id: UUID | None = None,
        user_id: UUID | None = None,
    ) -> list[ProjectMembership]:
        memberships = self.query_from_repository(
            loader=lambda repository: repository.query_project_memberships(
                project_id=project_id,
                user_id=user_id,
                limit=None,
                offset=0,
            ),
        )
        return sorted(memberships, key=lambda item: (item.project_id, item.created_at))

    def upsert_project_membership(
        self,
        project_id: UUID,
        user_id: UUID,
        role: ProjectMembershipRole,
        *,
        actor: AuthContext | None = None,
    ) -> ProjectMembership:
        self.authorization.require_owner(project_id, actor=actor)
        self.get_project(project_id)
        existing = self.get_project_membership_for_user(project_id, user_id)
        if existing is None:
            membership = ProjectMembership(
                membership_id=uuid4(),
                project_id=project_id,
                user_id=user_id,
                role=role,
                created_by=actor_user_id(actor),
            )
        else:
            membership = existing
            membership.role = role
            membership.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.project_memberships.save(membership)
        return membership

    def delete_project_membership(
        self,
        project_id: UUID,
        user_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> ProjectMembership:
        self.authorization.require_owner(project_id, actor=actor)
        membership = self.get_project_membership_for_user(project_id, user_id)
        if membership is None:
            raise NotFoundError("Project membership does not exist.")
        owner_count = sum(
            1
            for item in self.list_project_memberships(project_id=project_id)
            if item.role == ProjectMembershipRole.OWNER
        )
        if membership.role == ProjectMembershipRole.OWNER and owner_count <= 1:
            raise ValidationError("Projects must keep at least one owner.")
        with self.unit_of_work() as repository:
            repository.project_memberships.delete(membership.membership_id)
        return membership

    def accessible_project_ids(self, actor: AuthContext | None) -> set[UUID] | None:
        return self.authorization.accessible_project_ids(actor)

    def project_membership_role(
        self,
        project_id: UUID,
        actor: AuthContext | None,
    ) -> ProjectMembershipRole | None:
        return self.authorization.membership_role(project_id, actor)

    def require_project_read(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> None:
        self.authorization.require_read(project_id, actor=actor)

    def require_project_contributor(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> None:
        self.authorization.require_contributor(project_id, actor=actor)

    def require_project_owner(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> None:
        self.authorization.require_owner(project_id, actor=actor)
