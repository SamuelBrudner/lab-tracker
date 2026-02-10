"""Project domain service mixin."""

from __future__ import annotations

from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.models import Project, ProjectReviewPolicy, ProjectStatus, utc_now
from lab_tracker.services.shared import (
    WRITE_ROLES,
    _ensure_non_empty,
    _get_or_raise,
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
        created_by: str | None = None,
    ) -> Project:
        require_role(actor, WRITE_ROLES)
        _ensure_non_empty(name, "name")
        project = Project(
            project_id=uuid4(),
            name=name.strip(),
            description=description.strip(),
            status=status,
            review_policy=review_policy,
            created_by=created_by,
        )
        self._store.projects[project.project_id] = project
        self._run_repository_write(lambda repository: repository.projects.save(project))
        return project

    def get_project(self, project_id: UUID) -> Project:
        return _get_or_raise(self._store.projects, project_id, "Project")

    def list_projects(self) -> list[Project]:
        return list(self._store.projects.values())

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
        del self._store.projects[project_id]
        self._run_repository_write(lambda repository: repository.projects.delete(project_id))
        return project
