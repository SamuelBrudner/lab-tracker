"""Project mapping helpers between domain and SQLAlchemy models."""

from __future__ import annotations

from lab_tracker.db_models import ProjectGroupModel, ProjectModel
from lab_tracker.models import Project, ProjectGroup


def project_to_model(project: Project) -> ProjectModel:
    return ProjectModel(
        project_id=project.project_id,
        group_id=project.group_id if project.group_id is not None else None,
        name=project.name,
        description=project.description,
        status=project.status.value,
        created_by=project.created_by,
        created_by_user_id=(
            project.created_by_user_id
            if project.created_by_user_id is not None
            else None
        ),
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def project_from_model(row: ProjectModel) -> Project:
    return Project(
        project_id=row.project_id,
        group_id=row.group_id if row.group_id is not None else None,
        name=row.name,
        description=row.description,
        status=row.status,
        created_by=row.created_by,
        created_by_user_id=(
            row.created_by_user_id
            if row.created_by_user_id is not None
            else None
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def apply_project_to_model(row: ProjectModel, project: Project) -> None:
    row.group_id = project.group_id if project.group_id is not None else None
    row.name = project.name
    row.description = project.description
    row.status = project.status.value
    row.created_by = project.created_by
    row.created_by_user_id = (
        project.created_by_user_id
        if project.created_by_user_id is not None
        else None
    )
    row.created_at = project.created_at
    row.updated_at = project.updated_at


def project_group_to_model(group: ProjectGroup) -> ProjectGroupModel:
    return ProjectGroupModel(
        group_id=group.group_id,
        name=group.name,
        description=group.description,
        kind=group.kind.value,
        group_read_all=group.group_read_all,
        created_by=group.created_by,
        created_by_user_id=(
            group.created_by_user_id
            if group.created_by_user_id is not None
            else None
        ),
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


def project_group_from_model(row: ProjectGroupModel) -> ProjectGroup:
    return ProjectGroup(
        group_id=row.group_id,
        name=row.name,
        description=row.description,
        kind=row.kind,
        group_read_all=row.group_read_all,
        created_by=row.created_by,
        created_by_user_id=(
            row.created_by_user_id
            if row.created_by_user_id is not None
            else None
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def apply_project_group_to_model(row: ProjectGroupModel, group: ProjectGroup) -> None:
    row.name = group.name
    row.description = group.description
    row.kind = group.kind.value
    row.group_read_all = group.group_read_all
    row.created_by = group.created_by
    row.created_by_user_id = (
        group.created_by_user_id
        if group.created_by_user_id is not None
        else None
    )
    row.created_at = group.created_at
    row.updated_at = group.updated_at
