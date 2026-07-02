"""Project-scoped authorization policy."""

from __future__ import annotations

from uuid import UUID

from lab_tracker.auth import AuthContext, Role
from lab_tracker.errors import AuthError
from lab_tracker.models import ProjectMembershipRole
from lab_tracker.services.base import BaseService, ServiceContext

GROUP_READ_ROLES = {
    ProjectMembershipRole.VIEWER,
    ProjectMembershipRole.CONTRIBUTOR,
    ProjectMembershipRole.OWNER,
}
GROUP_OWNER_ROLES = {ProjectMembershipRole.OWNER}
PROJECT_READ_ROLES = {
    ProjectMembershipRole.VIEWER,
    ProjectMembershipRole.CONTRIBUTOR,
    ProjectMembershipRole.OWNER,
}
PROJECT_CONTRIBUTOR_ROLES = {
    ProjectMembershipRole.CONTRIBUTOR,
    ProjectMembershipRole.OWNER,
}
PROJECT_OWNER_ROLES = {ProjectMembershipRole.OWNER}


class ProjectAuthorizationPolicy(BaseService):
    def __init__(self, context: ServiceContext) -> None:
        super().__init__(context)

    def has_global_read(self, actor: AuthContext | None) -> bool:
        return actor is not None and actor.role == Role.ADMIN

    def has_global_write(self, actor: AuthContext | None) -> bool:
        return actor is not None and actor.role == Role.ADMIN

    def has_global_admin(self, actor: AuthContext | None) -> bool:
        return actor is not None and actor.role == Role.ADMIN

    def require_interactive(self, actor: AuthContext | None, *, action: str) -> None:
        """Reject non-interactive and unauthenticated principals at accept/commit gates.

        A delegated service token or an automation principal (e.g. the
        daily-review scheduler) may DRAFT graph proposals but must never accept
        or commit them: only a person in the loop turns a proposal into a
        committed graph edge. Pairs with :attr:`AuthContext.is_interactive`, and
        is fail-closed: only an interactive human session (USER or DEVICE) is
        admitted; SERVICE and SYSTEM principals and a missing (None) actor are
        all rejected.
        """

        if actor is None or not actor.is_interactive:
            raise AuthError(
                f"{action} requires an interactive human session; service "
                "tokens and automation principals may draft graph proposals "
                "but not accept or commit them."
            )

    def group_membership_role(
        self,
        group_id: UUID,
        actor: AuthContext | None,
    ) -> ProjectMembershipRole | None:
        if actor is None:
            raise AuthError("Authentication required.")
        if self.has_global_read(actor):
            return ProjectMembershipRole.OWNER
        membership = self.repository.get_group_membership(
            group_id=group_id,
            user_id=actor.user_id,
        )
        return membership.role if membership is not None else None

    def require_group_read(
        self,
        group_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> None:
        role = self.group_membership_role(group_id, actor)
        if role not in GROUP_READ_ROLES:
            raise AuthError("Group access required.")

    def require_group_owner(
        self,
        group_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> None:
        if self.has_global_admin(actor):
            return
        role = self.group_membership_role(group_id, actor)
        if role not in GROUP_OWNER_ROLES:
            raise AuthError("Group owner access required.")

    def accessible_project_ids(self, actor: AuthContext | None) -> set[UUID] | None:
        if self.has_global_read(actor):
            return None
        if actor is None:
            raise AuthError("Authentication required.")
        direct_memberships = self.query_from_repository(
            loader=lambda repository: repository.query_project_memberships(
                project_id=None,
                user_id=actor.user_id,
                limit=None,
                offset=0,
            ),
        )
        project_ids = {membership.project_id for membership in direct_memberships}
        group_memberships = self.query_from_repository(
            loader=lambda repository: repository.query_group_memberships(
                group_id=None,
                user_id=actor.user_id,
                limit=None,
                offset=0,
            ),
        )
        for group_membership in group_memberships:
            group = self.repository.project_groups.get(group_membership.group_id)
            if group is None:
                continue
            inherits_read = (
                group_membership.role == ProjectMembershipRole.OWNER or group.group_read_all
            )
            if not inherits_read:
                continue
            group_id = group_membership.group_id
            projects = self.query_from_repository(
                loader=lambda repository, group_id=group_id: repository.query_projects(
                    group_id=group_id,
                    limit=None,
                    offset=0,
                ),
            )
            project_ids.update(project.project_id for project in projects)
        return project_ids

    def membership_role(
        self,
        project_id: UUID,
        actor: AuthContext | None,
    ) -> ProjectMembershipRole | None:
        if actor is None:
            raise AuthError("Authentication required.")
        if self.has_global_read(actor):
            return ProjectMembershipRole.OWNER
        membership = self.repository.get_project_membership(
            project_id=project_id,
            user_id=actor.user_id,
        )
        if membership is not None:
            return membership.role
        project = self.repository.projects.get(project_id)
        if project is None or project.group_id is None:
            return None
        group_membership = self.repository.get_group_membership(
            group_id=project.group_id,
            user_id=actor.user_id,
        )
        if group_membership is None:
            return None
        if group_membership.role == ProjectMembershipRole.OWNER:
            return ProjectMembershipRole.OWNER
        group = self.repository.project_groups.get(project.group_id)
        if group is not None and group.group_read_all:
            return ProjectMembershipRole.VIEWER
        return None

    def require_read(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> None:
        role = self.membership_role(project_id, actor)
        if role not in PROJECT_READ_ROLES:
            raise AuthError("Project access required.")

    def require_contributor(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> None:
        if self.has_global_write(actor):
            return
        role = self.membership_role(project_id, actor)
        if role not in PROJECT_CONTRIBUTOR_ROLES:
            raise AuthError("Project contributor access required.")

    def require_owner(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> None:
        if self.has_global_admin(actor):
            return
        role = self.membership_role(project_id, actor)
        if role not in PROJECT_OWNER_ROLES:
            raise AuthError("Project owner access required.")
