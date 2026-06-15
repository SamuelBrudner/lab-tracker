"""Portfolio summary routes."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import (
    AnalysisStatus,
    ClaimStatus,
    DatasetStatus,
    Project,
    ProjectMembershipRole,
    ProjectStatus,
    QuestionStatus,
)
from lab_tracker.repository import LabTrackerRepository
from lab_tracker.schemas import (
    ListEnvelope,
    PortfolioProjectOwner,
    PortfolioProjectSummary,
)

from .shared import (
    filter_project_scoped_items,
    list_response,
    paginate,
    repository_from_request,
    validate_pagination,
)

QueryCount = Callable[..., tuple[list[Any], int]]


def build_portfolio_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/portfolio/summary", response_model=ListEnvelope[PortfolioProjectSummary])
    def portfolio_summary(
        request: Request,
        status: ProjectStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        repository = repository_from_request(request)
        projects, _ = repository.query_projects(
            status=status.value if status is not None else None,
            limit=None,
            offset=0,
        )
        visible_projects = filter_project_scoped_items(request, projects)
        page_projects, total = paginate(visible_projects, limit, offset)
        rows = [_summary_for_project(repository, project) for project in page_projects]
        return list_response(rows, limit=limit, offset=offset, total=total)

    return router


def _summary_for_project(
    repository: LabTrackerRepository,
    project: Project,
) -> PortfolioProjectSummary:
    project_id = project.project_id
    return PortfolioProjectSummary(
        project_id=project_id,
        name=project.name,
        status=project.status,
        open_question_count=_count_statuses(
            repository.query_questions,
            project_id,
            (QuestionStatus.STAGED.value, QuestionStatus.ACTIVE.value),
        ),
        draft_dataset_count=_count_statuses(
            repository.query_datasets,
            project_id,
            (DatasetStatus.STAGED.value,),
        ),
        committed_dataset_count=_count_statuses(
            repository.query_datasets,
            project_id,
            (DatasetStatus.COMMITTED.value,),
        ),
        running_analysis_count=_count_statuses(
            repository.query_analyses,
            project_id,
            (AnalysisStatus.STAGED.value,),
        ),
        unreviewed_claim_count=_count_statuses(
            repository.query_claims,
            project_id,
            (ClaimStatus.PROPOSED.value,),
        ),
        last_activity_at=_latest_activity_at(repository, project),
        owners=_owners_for_project(repository, project_id),
    )


def _count_statuses(
    query: QueryCount,
    project_id: UUID,
    statuses: Iterable[str],
) -> int:
    total = 0
    for status in statuses:
        _, status_total = query(
            project_id=project_id,
            status=status,
            limit=1,
            offset=0,
        )
        total += status_total
    return total


def _latest_activity_at(
    repository: LabTrackerRepository,
    project: Project,
) -> datetime | None:
    candidates: list[datetime] = []
    for value in (project.created_at, project.updated_at):
        if value is not None:
            candidates.append(value)
    for query in (
        repository.query_questions,
        repository.query_datasets,
        repository.query_notes,
        repository.query_sessions,
        repository.query_analyses,
        repository.query_claims,
        repository.query_goals,
        repository.query_visualizations,
        repository.query_graph_change_sets,
    ):
        items, _ = query(project_id=project.project_id, limit=None, offset=0)
        candidates.extend(_activity_timestamps(items))
    return max(candidates) if candidates else None


def _activity_timestamps(items: Iterable[Any]) -> Iterable[datetime]:
    for item in items:
        for field_name in ("updated_at", "created_at"):
            value = getattr(item, field_name, None)
            if value is not None:
                yield value


def _owners_for_project(
    repository: LabTrackerRepository,
    project_id: UUID,
) -> list[PortfolioProjectOwner]:
    memberships, _ = repository.query_project_memberships(
        project_id=project_id,
        limit=None,
        offset=0,
    )
    return [
        PortfolioProjectOwner(user_id=membership.user_id, username=membership.username)
        for membership in memberships
        if membership.role == ProjectMembershipRole.OWNER
    ]
