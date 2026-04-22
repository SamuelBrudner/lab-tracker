"""Project domain service mixin."""

from __future__ import annotations

from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.models import Project, ProjectReviewPolicy, ProjectStatus, utc_now
from lab_tracker.services.shared import (
    WRITE_ROLES,
    _actor_user_id,
    _ensure_non_empty,
)


class ProjectServiceMixin:
    def create_project(
        self,
        name: str,
        description: str = "",
        status: ProjectStatus = ProjectStatus.ACTIVE,
        review_policy: ProjectReviewPolicy = ProjectReviewPolicy.NONE,
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
            review_policy=review_policy,
            created_by=_actor_user_id(actor),
        )
        self._remember_entity("projects", project.project_id, project)
        self._run_repository_write(lambda repository: repository.projects.save(project))
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
        review_policy: ProjectReviewPolicy | None = None,
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
        if review_policy is not None:
            project.review_policy = review_policy
        project.updated_at = utc_now()
        self._run_repository_write(lambda repository: repository.projects.save(project))
        return project

    def delete_project(self, project_id: UUID, *, actor: AuthContext | None = None) -> Project:
        require_role(actor, WRITE_ROLES)
        project = self.get_project(project_id)
        self._forget_entity("projects", project_id)
        self._run_repository_write(lambda repository: repository.projects.delete(project_id))
        return project
