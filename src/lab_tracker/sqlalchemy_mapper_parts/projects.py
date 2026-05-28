"""Project mapping helpers between domain and SQLAlchemy models."""

from __future__ import annotations

from lab_tracker.db_models import ProjectModel
from lab_tracker.models import Project, ProjectStatus
from lab_tracker.sqlalchemy_mapper_parts.common import as_utc, uuid_from_db, uuid_to_db


def project_to_model(project: Project) -> ProjectModel:
    return ProjectModel(
        project_id=uuid_to_db(project.project_id),
        name=project.name,
        description=project.description,
        status=project.status.value,
        created_by=project.created_by,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def project_from_model(row: ProjectModel) -> Project:
    return Project(
        project_id=uuid_from_db(row.project_id),
        name=row.name,
        description=row.description,
        status=ProjectStatus(row.status),
        created_by=row.created_by,
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
    )


def apply_project_to_model(row: ProjectModel, project: Project) -> None:
    row.name = project.name
    row.description = project.description
    row.status = project.status.value
    row.created_by = project.created_by
    row.created_at = project.created_at
    row.updated_at = project.updated_at
