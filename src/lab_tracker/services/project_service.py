"""Project domain service mixin."""

from __future__ import annotations

from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.errors import AuthError, NotFoundError, ValidationError
from lab_tracker.models import (
    Project,
    ProjectMembership,
    ProjectMembershipRole,
    ProjectStatus,
    utc_now,
)
from lab_tracker.services.shared import (
    PROJECT_CONTRIBUTOR_ROLES,
    PROJECT_OWNER_ROLES,
    PROJECT_READ_ROLES,
    WRITE_ROLES,
    _actor_user_id,
    _ensure_non_empty,
    has_global_project_admin,
    has_global_project_read,
    has_global_project_write,
)


class ProjectServiceMixin:
    def create_project(
        self,
        name: str,
        description: str = "",
        status: ProjectStatus = ProjectStatus.ACTIVE,
        *,
        actor: AuthContext | None = None,
    ) -> Project:
        require_role(actor, WRITE_ROLES)
        _ensure_non_empty(name, "name")
        project = Project(
            project_id=uuid4(),
            name=name.strip(),
            description=description.strip(),
            status=status,
            created_by=_actor_user_id(actor),
        )
        self._remember_entity("projects", project.project_id, project)
        self._run_repository_write(lambda repository: repository.projects.save(project))
        if actor is not None and not has_global_project_admin(actor):
            membership = ProjectMembership(
                membership_id=uuid4(),
                project_id=project.project_id,
                user_id=actor.user_id,
                role=ProjectMembershipRole.OWNER,
                created_by=_actor_user_id(actor),
            )
            self._remember_entity(
                "project_memberships",
                membership.membership_id,
                membership,
            )
            self._run_repository_write(
                lambda repository: repository.project_memberships.save(membership)
            )
        return project

    def get_project(self, project_id: UUID) -> Project:
        return self._get_from_repository_or_store(
            attribute_name="projects",
            entity_id=project_id,
            label="Project",
            loader=lambda repository: repository.projects.get(project_id),
        )

    def list_projects(self) -> list[Project]:
        return self._list_from_repository_or_store(
            attribute_name="projects",
            loader=lambda repository: repository.projects.list(),
            entity_id_getter=lambda project: project.project_id,
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
            _ensure_non_empty(name, "name")
            project.name = name.strip()
        if description is not None:
            project.description = description.strip()
        if status is not None:
            project.status = status
        project.updated_at = utc_now()
        self._run_repository_write(lambda repository: repository.projects.save(project))
        return project

    def delete_project(self, project_id: UUID, *, actor: AuthContext | None = None) -> Project:
        require_role(actor, WRITE_ROLES)
        project = self.get_project(project_id)
        self._forget_entity("projects", project_id)
        self._run_repository_write(lambda repository: repository.projects.delete(project_id))
        return project

    def get_project_membership(self, membership_id: UUID) -> ProjectMembership:
        return self._get_from_repository_or_store(
            attribute_name="project_memberships",
            entity_id=membership_id,
            label="Project membership",
            loader=lambda repository: repository.project_memberships.get(membership_id),
        )

    def get_project_membership_for_user(
        self,
        project_id: UUID,
        user_id: UUID,
    ) -> ProjectMembership | None:
        repository = self._active_repository()
        if repository is not None and not self._allow_in_memory:
            return repository.get_project_membership(project_id=project_id, user_id=user_id)
        for membership in self._store.project_memberships.values():
            if membership.project_id == project_id and membership.user_id == user_id:
                return membership
        return None

    def list_project_memberships(
        self,
        *,
        project_id: UUID | None = None,
        user_id: UUID | None = None,
    ) -> list[ProjectMembership]:
        memberships = self._query_from_repository(
            attribute_name="project_memberships",
            loader=lambda repository: repository.query_project_memberships(
                project_id=project_id,
                user_id=user_id,
                limit=None,
                offset=0,
            ),
            entity_id_getter=lambda membership: membership.membership_id,
        )
        if memberships is None:
            memberships = list(self._store.project_memberships.values())
        if project_id is not None:
            memberships = [
                membership
                for membership in memberships
                if membership.project_id == project_id
            ]
        if user_id is not None:
            memberships = [
                membership for membership in memberships if membership.user_id == user_id
            ]
        return sorted(memberships, key=lambda item: (item.project_id, item.created_at))

    def upsert_project_membership(
        self,
        project_id: UUID,
        user_id: UUID,
        role: ProjectMembershipRole,
        *,
        actor: AuthContext | None = None,
    ) -> ProjectMembership:
        self.require_project_owner(project_id, actor=actor)
        self.get_project(project_id)
        existing = self.get_project_membership_for_user(project_id, user_id)
        if existing is None:
            membership = ProjectMembership(
                membership_id=uuid4(),
                project_id=project_id,
                user_id=user_id,
                role=role,
                created_by=_actor_user_id(actor),
            )
        else:
            membership = existing
            membership.role = role
            membership.updated_at = utc_now()
        self._remember_entity("project_memberships", membership.membership_id, membership)
        self._run_repository_write(
            lambda repository: repository.project_memberships.save(membership)
        )
        return membership

    def delete_project_membership(
        self,
        project_id: UUID,
        user_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> ProjectMembership:
        self.require_project_owner(project_id, actor=actor)
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
        self._forget_entity("project_memberships", membership.membership_id)
        self._run_repository_write(
            lambda repository: repository.project_memberships.delete(membership.membership_id)
        )
        return membership

    def accessible_project_ids(self, actor: AuthContext | None) -> set[UUID] | None:
        if has_global_project_read(actor):
            return None
        if actor is None:
            raise AuthError("Authentication required.")
        return {
            membership.project_id
            for membership in self.list_project_memberships(user_id=actor.user_id)
        }

    def project_membership_role(
        self,
        project_id: UUID,
        actor: AuthContext | None,
    ) -> ProjectMembershipRole | None:
        if actor is None:
            raise AuthError("Authentication required.")
        if has_global_project_read(actor):
            return ProjectMembershipRole.OWNER
        membership = self.get_project_membership_for_user(project_id, actor.user_id)
        return membership.role if membership is not None else None

    def require_project_read(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> None:
        role = self.project_membership_role(project_id, actor)
        if role not in PROJECT_READ_ROLES:
            raise AuthError("Project access required.")

    def require_project_contributor(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> None:
        if has_global_project_write(actor):
            return
        role = self.project_membership_role(project_id, actor)
        if role not in PROJECT_CONTRIBUTOR_ROLES:
            raise AuthError("Project contributor access required.")

    def require_project_owner(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> None:
        if has_global_project_admin(actor):
            return
        if actor is None:
            raise AuthError("Authentication required.")
        membership = self.get_project_membership_for_user(project_id, actor.user_id)
        role = membership.role if membership is not None else None
        if role not in PROJECT_OWNER_ROLES:
            raise AuthError("Project owner access required.")
