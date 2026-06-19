"""Project domain service."""

from __future__ import annotations

from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.errors import AuthError, NotFoundError, ValidationError
from lab_tracker.models import (
    GroupMembership,
    Project,
    ProjectGroup,
    ProjectGroupKind,
    ProjectMembership,
    ProjectMembershipRole,
    ProjectStatus,
    utc_now,
)
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.goal_link_cleanup import remove_goal_links_to_project_contents
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.shared import (
    WRITE_ROLES,
    actor_user_fk,
    actor_user_id,
    ensure_non_empty,
)

_GROUP_ID_UNSET = object()


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
        group_id: UUID | None = None,
        *,
        actor: AuthContext | None = None,
    ) -> Project:
        require_role(actor, WRITE_ROLES)
        if group_id is not None:
            self.authorization.require_group_owner(group_id, actor=actor)
            self.get_project_group(group_id)
        ensure_non_empty(name, "name")
        project = Project(
            project_id=uuid4(),
            group_id=group_id,
            name=name.strip(),
            description=description.strip(),
            status=status,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
        )
        with self.unit_of_work() as repository:
            repository.projects.save(project)
            # Flush the project before the request-managed owner membership insert.
            repository.projects.get(project.project_id)
        if actor is not None and not self.authorization.has_global_admin(actor):
            membership = ProjectMembership(
                membership_id=uuid4(),
                project_id=project.project_id,
                user_id=actor.user_id,
                role=ProjectMembershipRole.OWNER,
                created_by=actor_user_id(actor),
                created_by_user_id=actor_user_fk(actor, self.repository),
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
        group_id: UUID | None | object = _GROUP_ID_UNSET,
        actor: AuthContext | None = None,
    ) -> Project:
        self.authorization.require_owner(project_id, actor=actor)
        project = self.get_project(project_id)
        if group_id is not _GROUP_ID_UNSET:
            if group_id is not None:
                self.authorization.require_group_owner(group_id, actor=actor)
                self.get_project_group(group_id)
            project.group_id = group_id
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
        self.authorization.require_owner(project_id, actor=actor)
        project = self.get_project(project_id)
        with self.unit_of_work() as repository:
            remove_goal_links_to_project_contents(repository, project_id=project_id)
            repository.projects.delete(project_id)
        return project

    def create_project_group(
        self,
        name: str,
        description: str = "",
        kind: ProjectGroupKind = ProjectGroupKind.LAB,
        group_read_all: bool = False,
        *,
        actor: AuthContext | None = None,
    ) -> ProjectGroup:
        require_role(actor, WRITE_ROLES)
        ensure_non_empty(name, "name")
        group = ProjectGroup(
            group_id=uuid4(),
            name=name.strip(),
            description=description.strip(),
            kind=kind,
            group_read_all=group_read_all,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
        )
        with self.unit_of_work() as repository:
            repository.project_groups.save(group)
            repository.project_groups.get(group.group_id)
        if actor is not None and not self.authorization.has_global_admin(actor):
            membership = GroupMembership(
                membership_id=uuid4(),
                group_id=group.group_id,
                user_id=actor.user_id,
                role=ProjectMembershipRole.OWNER,
                created_by=actor_user_id(actor),
                created_by_user_id=actor_user_fk(actor, self.repository),
            )
            with self.unit_of_work() as repository:
                repository.group_memberships.save(membership)
        return group

    def get_project_group(self, group_id: UUID) -> ProjectGroup:
        return self.get_from_repository(
            entity_id=group_id,
            label="Project group",
            loader=lambda repository: repository.project_groups.get(group_id),
        )

    def list_project_groups(
        self,
        *,
        actor: AuthContext | None = None,
    ) -> list[ProjectGroup]:
        if self.authorization.has_global_read(actor):
            return self.list_from_repository(
                loader=lambda repository: repository.project_groups.list(),
            )
        if actor is None:
            raise AuthError("Authentication required.")
        memberships = self.query_from_repository(
            loader=lambda repository: repository.query_group_memberships(
                user_id=actor.user_id,
                limit=None,
                offset=0,
            ),
        )
        groups = [
            group
            for membership in memberships
            if (group := self.repository.project_groups.get(membership.group_id)) is not None
        ]
        return sorted(groups, key=lambda item: (item.created_at, item.group_id))

    def update_project_group(
        self,
        group_id: UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        kind: ProjectGroupKind | None = None,
        group_read_all: bool | None = None,
        actor: AuthContext | None = None,
    ) -> ProjectGroup:
        self.authorization.require_group_owner(group_id, actor=actor)
        group = self.get_project_group(group_id)
        if name is not None:
            ensure_non_empty(name, "name")
            group.name = name.strip()
        if description is not None:
            group.description = description.strip()
        if kind is not None:
            group.kind = kind
        if group_read_all is not None:
            group.group_read_all = group_read_all
        group.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.project_groups.save(group)
        return group

    def delete_project_group(
        self,
        group_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> ProjectGroup:
        self.authorization.require_group_owner(group_id, actor=actor)
        group = self.get_project_group(group_id)
        with self.unit_of_work() as repository:
            repository.project_groups.delete(group_id)
        return group

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

    def get_group_membership(self, membership_id: UUID) -> GroupMembership:
        return self.get_from_repository(
            entity_id=membership_id,
            label="Group membership",
            loader=lambda repository: repository.group_memberships.get(membership_id),
        )

    def get_group_membership_for_user(
        self,
        group_id: UUID,
        user_id: UUID,
    ) -> GroupMembership | None:
        return self.repository.get_group_membership(
            group_id=group_id,
            user_id=user_id,
        )

    def list_group_memberships(
        self,
        *,
        group_id: UUID | None = None,
        user_id: UUID | None = None,
        actor: AuthContext | None = None,
    ) -> list[GroupMembership]:
        if group_id is not None:
            self.authorization.require_group_owner(group_id, actor=actor)
        memberships = self.query_from_repository(
            loader=lambda repository: repository.query_group_memberships(
                group_id=group_id,
                user_id=user_id,
                limit=None,
                offset=0,
            ),
        )
        return sorted(memberships, key=lambda item: (item.group_id, item.created_at))

    def upsert_group_membership(
        self,
        group_id: UUID,
        user_id: UUID,
        role: ProjectMembershipRole,
        *,
        actor: AuthContext | None = None,
    ) -> GroupMembership:
        self.authorization.require_group_owner(group_id, actor=actor)
        self.get_project_group(group_id)
        existing = self.get_group_membership_for_user(group_id, user_id)
        if existing is None:
            membership = GroupMembership(
                membership_id=uuid4(),
                group_id=group_id,
                user_id=user_id,
                role=role,
                created_by=actor_user_id(actor),
                created_by_user_id=actor_user_fk(actor, self.repository),
            )
        else:
            membership = existing
            membership.role = role
            membership.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.group_memberships.save(membership)
            saved = repository.group_memberships.get(membership.membership_id)
        return saved or membership

    def delete_group_membership(
        self,
        group_id: UUID,
        user_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> GroupMembership:
        self.authorization.require_group_owner(group_id, actor=actor)
        membership = self.get_group_membership_for_user(group_id, user_id)
        if membership is None:
            raise NotFoundError("Group membership does not exist.")
        owner_count = sum(
            1
            for item in self.list_group_memberships(group_id=group_id, actor=actor)
            if item.role == ProjectMembershipRole.OWNER
        )
        if membership.role == ProjectMembershipRole.OWNER and owner_count <= 1:
            raise ValidationError("Groups must keep at least one owner.")
        project_ids = self._project_ids_for_group(group_id)
        self._ensure_offboarding_records_released(user_id, project_ids)
        with self.unit_of_work() as repository:
            repository.group_memberships.delete(membership.membership_id)
        return membership

    def upsert_group_project_memberships(
        self,
        group_id: UUID,
        user_id: UUID,
        role: ProjectMembershipRole,
        *,
        actor: AuthContext | None = None,
    ) -> list[ProjectMembership]:
        self.authorization.require_group_owner(group_id, actor=actor)
        self.get_project_group(group_id)
        projects = self.query_from_repository(
            loader=lambda repository: repository.query_projects(
                group_id=group_id,
                limit=None,
                offset=0,
            ),
        )
        memberships: list[ProjectMembership] = []
        with self.unit_of_work() as repository:
            sole_owner_project_ids: list[UUID] = []
            for project in projects:
                existing = repository.get_project_membership(
                    project_id=project.project_id,
                    user_id=user_id,
                )
                if existing is None:
                    membership = ProjectMembership(
                        membership_id=uuid4(),
                        project_id=project.project_id,
                        user_id=user_id,
                        role=role,
                        created_by=actor_user_id(actor),
                        created_by_user_id=actor_user_fk(actor, self.repository),
                    )
                else:
                    membership = existing
                    if membership.role == ProjectMembershipRole.OWNER and role != membership.role:
                        repository.lock_project_owner_memberships(project.project_id)
                        membership = repository.get_project_membership(
                            project_id=project.project_id,
                            user_id=user_id,
                        )
                        if membership is None:
                            continue
                        if (
                            membership.role == ProjectMembershipRole.OWNER
                            and self._project_owner_count(repository, project.project_id) <= 1
                        ):
                            sole_owner_project_ids.append(project.project_id)
                            continue
                    membership.role = role
                    membership.updated_at = utc_now()
                if sole_owner_project_ids:
                    continue
                repository.project_memberships.save(membership)
                saved = repository.project_memberships.get(membership.membership_id)
                memberships.append(saved or membership)
            if sole_owner_project_ids:
                project_list = ", ".join(str(project_id) for project_id in sole_owner_project_ids)
                raise ValidationError(
                    f"Cannot demote the last owner from projects: {project_list}."
                )
        return memberships

    def delete_group_project_memberships(
        self,
        group_id: UUID,
        user_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> list[ProjectMembership]:
        self.authorization.require_group_owner(group_id, actor=actor)
        self.get_project_group(group_id)
        memberships: list[ProjectMembership] = []
        sole_owner_project_ids: list[UUID] = []
        with self.unit_of_work() as repository:
            projects, _ = repository.query_projects(
                group_id=group_id,
                limit=None,
                offset=0,
            )
            project_ids = {project.project_id for project in projects}
            for project in projects:
                membership = repository.get_project_membership(
                    project_id=project.project_id,
                    user_id=user_id,
                )
                if membership is None:
                    continue
                if membership.role == ProjectMembershipRole.OWNER:
                    repository.lock_project_owner_memberships(project.project_id)
                    refreshed = repository.get_project_membership(
                        project_id=project.project_id,
                        user_id=user_id,
                    )
                    if refreshed is None:
                        continue
                    membership = refreshed
                    if (
                        membership.role == ProjectMembershipRole.OWNER
                        and self._project_owner_count(repository, project.project_id) <= 1
                    ):
                        sole_owner_project_ids.append(project.project_id)
                        continue
                memberships.append(membership)
            if sole_owner_project_ids:
                project_list = ", ".join(str(project_id) for project_id in sole_owner_project_ids)
                raise ValidationError(
                    f"Cannot remove the last owner from projects: {project_list}."
                )
            self._ensure_offboarding_records_released(user_id, project_ids)
            for membership in memberships:
                repository.project_memberships.delete(membership.membership_id)
        return memberships

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
        with self.unit_of_work() as repository:
            membership = repository.get_project_membership(
                project_id=project_id,
                user_id=user_id,
            )
            if membership is None:
                membership = ProjectMembership(
                    membership_id=uuid4(),
                    project_id=project_id,
                    user_id=user_id,
                    role=role,
                    created_by=actor_user_id(actor),
                    created_by_user_id=actor_user_fk(actor, self.repository),
                )
            else:
                membership = self._update_existing_project_membership_role(
                    repository,
                    project_id=project_id,
                    membership=membership,
                    role=role,
                )
            repository.project_memberships.save(membership)
        return membership

    def update_project_membership(
        self,
        project_id: UUID,
        user_id: UUID,
        role: ProjectMembershipRole,
        *,
        actor: AuthContext | None = None,
    ) -> ProjectMembership:
        self.authorization.require_owner(project_id, actor=actor)
        self.get_project(project_id)
        with self.unit_of_work() as repository:
            membership = repository.get_project_membership(
                project_id=project_id,
                user_id=user_id,
            )
            if membership is None:
                raise NotFoundError("Project membership does not exist.")
            membership = self._update_existing_project_membership_role(
                repository,
                project_id=project_id,
                membership=membership,
                role=role,
            )
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
        with self.unit_of_work() as repository:
            membership = repository.get_project_membership(
                project_id=project_id,
                user_id=user_id,
            )
            if membership is None:
                raise NotFoundError("Project membership does not exist.")
            if membership.role == ProjectMembershipRole.OWNER:
                repository.lock_project_owner_memberships(project_id)
                if self._project_owner_count(repository, project_id) <= 1:
                    raise ValidationError("Projects must keep at least one owner.")
            self._ensure_offboarding_records_released(user_id, {project_id})
            repository.project_memberships.delete(membership.membership_id)
        return membership

    @staticmethod
    def _project_owner_count(repository, project_id: UUID) -> int:  # noqa: ANN001
        memberships, _ = repository.query_project_memberships(
            project_id=project_id,
            limit=None,
            offset=0,
        )
        return sum(
            1 for membership in memberships if membership.role == ProjectMembershipRole.OWNER
        )

    def _update_existing_project_membership_role(
        self,
        repository,  # noqa: ANN001
        *,
        project_id: UUID,
        membership: ProjectMembership,
        role: ProjectMembershipRole,
    ) -> ProjectMembership:
        if membership.role == ProjectMembershipRole.OWNER and role != membership.role:
            repository.lock_project_owner_memberships(project_id)
            refreshed = repository.get_project_membership(
                project_id=project_id,
                user_id=membership.user_id,
            )
            if refreshed is None:
                raise NotFoundError("Project membership does not exist.")
            membership = refreshed
            if (
                membership.role == ProjectMembershipRole.OWNER
                and self._project_owner_count(repository, project_id) <= 1
            ):
                raise ValidationError("Projects must keep at least one owner.")
        membership.role = role
        membership.updated_at = utc_now()
        return membership

    def _project_ids_for_group(self, group_id: UUID) -> set[UUID]:
        projects, _ = self.repository.query_projects(
            group_id=group_id,
            limit=None,
            offset=0,
        )
        return {project.project_id for project in projects}

    def _ensure_offboarding_records_released(
        self,
        user_id: UUID,
        candidate_project_ids: set[UUID],
    ) -> None:
        record_project_ids = self._attributed_record_project_ids(
            user_id,
            candidate_project_ids,
        )
        if not record_project_ids:
            return
        export_events, _ = self.repository.query_record_export_events(
            user_id=user_id,
            limit=None,
            offset=0,
        )
        for event in export_events:
            if record_project_ids.issubset(set(event.project_ids)):
                return
        project_list = ", ".join(str(project_id) for project_id in sorted(record_project_ids))
        raise ValidationError(
            "Export or reassign this user's records before revoking membership "
            f"for projects: {project_list}."
        )

    def _attributed_record_project_ids(
        self,
        user_id: UUID,
        candidate_project_ids: set[UUID],
    ) -> set[UUID]:
        if not candidate_project_ids:
            return set()
        records = self.repository.records_attributed_to_user(
            user_id=user_id,
            project_ids=candidate_project_ids,
        )
        project_ids = {
            item.project_id
            for collection in (
                records.questions,
                records.datasets,
                records.sessions,
                records.notes,
                records.analyses,
                records.claims,
            )
            for item in collection
        }
        for visualization in records.visualizations:
            analysis = self.repository.analyses.get(visualization.analysis_id)
            if analysis is not None:
                project_ids.add(analysis.project_id)
        return project_ids

    def accessible_project_ids(self, actor: AuthContext | None) -> set[UUID] | None:
        return self.authorization.accessible_project_ids(actor)

    def project_membership_role(
        self,
        project_id: UUID,
        actor: AuthContext | None,
    ) -> ProjectMembershipRole | None:
        return self.authorization.membership_role(project_id, actor)

    def group_membership_role(
        self,
        group_id: UUID,
        actor: AuthContext | None,
    ) -> ProjectMembershipRole | None:
        return self.authorization.group_membership_role(group_id, actor)

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

    def require_group_read(
        self,
        group_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> None:
        self.authorization.require_group_read(group_id, actor=actor)

    def require_group_owner(
        self,
        group_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> None:
        self.authorization.require_group_owner(group_id, actor=actor)
