"""Portfolio summary routes."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import (
    AnalysisStatus,
    ClaimStatus,
    DatasetStatus,
    GoalStatus,
    Project,
    ProjectMembershipRole,
    ProjectStatus,
    QuestionStatus,
)
from lab_tracker.repository import LabTrackerRepository
from lab_tracker.schemas import (
    ListEnvelope,
    PortfolioProjectGroupSummary,
    PortfolioProjectOwner,
    PortfolioProjectSummary,
    PortfolioTriageFlag,
)
from lab_tracker.sqlalchemy_mapper_parts.common import as_utc

from .shared import (
    filter_project_scoped_items,
    list_response,
    paginate,
    repository_from_request,
    validate_pagination,
)

QueryCount = Callable[..., tuple[list[Any], int]]
STALE_PROJECT_AFTER = timedelta(days=30)
OPEN_GOAL_STATUSES = {GoalStatus.PLANNED, GoalStatus.IN_PROGRESS}


def build_portfolio_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/portfolio/summary",
        response_model=ListEnvelope[PortfolioProjectGroupSummary],
    )
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
        summaries = [_summary_for_project(repository, project) for project in visible_projects]
        groups = _group_project_summaries(repository, visible_projects, summaries)
        page_groups, total = paginate(groups, limit, offset)
        return list_response(page_groups, limit=limit, offset=offset, total=total)

    return router


def _summary_for_project(
    repository: LabTrackerRepository,
    project: Project,
) -> PortfolioProjectSummary:
    project_id = project.project_id
    open_question_count = _count_statuses(
        repository.query_questions,
        project_id,
        (QuestionStatus.STAGED.value, QuestionStatus.ACTIVE.value),
    )
    draft_dataset_count = _count_statuses(
        repository.query_datasets,
        project_id,
        (DatasetStatus.STAGED.value,),
    )
    committed_dataset_count = _count_statuses(
        repository.query_datasets,
        project_id,
        (DatasetStatus.COMMITTED.value,),
    )
    staged_analysis_count = _count_statuses(
        repository.query_analyses,
        project_id,
        (AnalysisStatus.STAGED.value,),
    )
    unreviewed_claim_count = _count_statuses(
        repository.query_claims,
        project_id,
        (ClaimStatus.PROPOSED.value,),
    )
    last_activity_at = _latest_activity_at(repository, project)
    return PortfolioProjectSummary(
        project_id=project_id,
        name=project.name,
        status=project.status,
        open_question_count=open_question_count,
        draft_dataset_count=draft_dataset_count,
        committed_dataset_count=committed_dataset_count,
        staged_analysis_count=staged_analysis_count,
        unreviewed_claim_count=unreviewed_claim_count,
        last_activity_at=last_activity_at,
        owners=_owners_for_project(repository, project_id),
        triage_flags=_triage_flags_for_project(
            repository,
            project,
            open_question_count=open_question_count,
            unreviewed_claim_count=unreviewed_claim_count,
            last_activity_at=last_activity_at,
        ),
    )


def _group_project_summaries(
    repository: LabTrackerRepository,
    projects: list[Project],
    summaries: list[PortfolioProjectSummary],
) -> list[PortfolioProjectGroupSummary]:
    project_groups: dict[UUID | None, list[PortfolioProjectSummary]] = {}
    group_order: list[UUID | None] = []
    for project, summary in zip(projects, summaries, strict=True):
        if project.group_id not in project_groups:
            project_groups[project.group_id] = []
            group_order.append(project.group_id)
        project_groups[project.group_id].append(summary)
    return [
        PortfolioProjectGroupSummary(
            project_group=repository.project_groups.get(group_id)
            if group_id is not None
            else None,
            project_count=len(project_groups[group_id]),
            projects=project_groups[group_id],
        )
        for group_id in group_order
    ]


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
            candidates.append(as_utc(value))
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
                yield as_utc(value)


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


def _triage_flags_for_project(
    repository: LabTrackerRepository,
    project: Project,
    *,
    open_question_count: int,
    unreviewed_claim_count: int,
    last_activity_at: datetime | None,
) -> list[PortfolioTriageFlag]:
    project_id = project.project_id
    flags: list[PortfolioTriageFlag] = []
    if _is_stale_project(last_activity_at):
        flags.append(
            PortfolioTriageFlag(
                key="stale_project",
                label=f"No activity in {STALE_PROJECT_AFTER.days} days",
                count=1,
            )
        )
    if open_question_count:
        flags.append(
            PortfolioTriageFlag(
                key="unanswered_questions",
                label="Unanswered questions",
                count=open_question_count,
            )
        )

    datasets, _ = repository.query_datasets(
        project_id=project_id,
        status=DatasetStatus.COMMITTED.value,
        limit=None,
        offset=0,
    )
    analyses, _ = repository.query_analyses(project_id=project_id, limit=None, offset=0)
    active_analysis_dataset_ids = {
        dataset_id
        for analysis in analyses
        if analysis.status != AnalysisStatus.ARCHIVED
        for dataset_id in analysis.dataset_ids
    }
    dataset_gap_count = sum(
        1 for dataset in datasets if dataset.dataset_id not in active_analysis_dataset_ids
    )
    if dataset_gap_count:
        flags.append(
            PortfolioTriageFlag(
                key="datasets_without_analyses",
                label="Datasets without analyses",
                count=dataset_gap_count,
            )
        )

    claims, _ = repository.query_claims(project_id=project_id, limit=None, offset=0)
    active_claim_analysis_ids = {
        analysis_id
        for claim in claims
        if claim.status != ClaimStatus.REJECTED
        for analysis_id in claim.supported_by_analysis_ids
    }
    analysis_gap_count = sum(
        1
        for analysis in analyses
        if analysis.status == AnalysisStatus.COMMITTED
        and analysis.analysis_id not in active_claim_analysis_ids
    )
    if analysis_gap_count:
        flags.append(
            PortfolioTriageFlag(
                key="analyses_without_claims",
                label="Analyses without claims",
                count=analysis_gap_count,
            )
        )
    if unreviewed_claim_count:
        flags.append(
            PortfolioTriageFlag(
                key="unreviewed_claims",
                label="Unreviewed claims",
                count=unreviewed_claim_count,
                severity="info",
            )
        )

    goals, _ = repository.query_goals(project_id=project_id, limit=None, offset=0)
    today = datetime.now(timezone.utc).date()
    overdue_goal_count = sum(
        1
        for goal in goals
        if goal.target_date is not None
        and goal.target_date < today
        and goal.status in OPEN_GOAL_STATUSES
    )
    if overdue_goal_count:
        flags.append(
            PortfolioTriageFlag(
                key="overdue_goals",
                label="Overdue goals",
                count=overdue_goal_count,
                severity="critical",
            )
        )
    return flags


def _is_stale_project(last_activity_at: datetime | None) -> bool:
    if last_activity_at is None:
        return True
    return as_utc(last_activity_at) < datetime.now(timezone.utc) - STALE_PROJECT_AFTER
