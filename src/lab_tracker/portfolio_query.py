"""SQL-backed portfolio summary read model."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, literal, or_, select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import (
    AnalysisDatasetModel,
    AnalysisModel,
    ClaimAnalysisModel,
    ClaimModel,
    DatasetModel,
    GoalLinkModel,
    GoalModel,
    GraphChangeSetModel,
    NoteModel,
    ProjectGroupModel,
    ProjectMembershipModel,
    ProjectModel,
    QuestionModel,
    SessionModel,
    UserModel,
    VisualizationModel,
)
from lab_tracker.models import (
    AnalysisStatus,
    ClaimStatus,
    DatasetStatus,
    EntityType,
    GoalStatus,
    ProjectGroup,
    ProjectMembershipRole,
    ProjectStatus,
    QuestionStatus,
)
from lab_tracker.schemas import (
    PortfolioProjectGroupSummary,
    PortfolioProjectOwner,
    PortfolioProjectSummary,
    PortfolioTriageFlag,
)
from lab_tracker.sqlalchemy_mapper_parts.common import as_utc
from lab_tracker.sqlalchemy_mapper_parts.projects import project_group_from_model
from lab_tracker.sqlalchemy_repository_parts.common import apply_pagination, count_from_statement

STALE_PROJECT_AFTER = timedelta(days=30)
OPEN_GOAL_STATUSES = {GoalStatus.PLANNED.value, GoalStatus.IN_PROGRESS.value}


def portfolio_summary_groups(
    session: OrmSession,
    *,
    accessible_project_ids: set[UUID] | None,
    status: ProjectStatus | None,
    limit: int,
    offset: int,
) -> tuple[list[PortfolioProjectGroupSummary], int]:
    """Return portfolio summary groups using bounded aggregate SQL queries."""

    session.flush()
    accessible_ids = (
        _uuid_values(accessible_project_ids)
        if accessible_project_ids is not None
        else None
    )
    if accessible_ids == []:
        return [], 0

    project_filters = _project_filters(accessible_ids=accessible_ids, status=status)
    group_rows, total_groups = _page_visible_groups(
        session,
        project_filters=project_filters,
        limit=limit,
        offset=offset,
    )
    if not group_rows:
        return [], total_groups

    page_group_ids = [row.group_id for row in group_rows]
    project_rows = _projects_for_groups(
        session,
        project_filters=project_filters,
        group_ids=page_group_ids,
    )
    projects_by_group: dict[str | None, list[PortfolioProjectSummary]] = defaultdict(list)
    summaries_by_project = _summary_by_project(session, project_rows)
    for project in project_rows:
        projects_by_group[project.group_id].append(summaries_by_project[project.project_id])

    project_groups = _project_groups_by_id(
        session,
        (group_id for group_id in page_group_ids if group_id is not None),
    )
    groups = [
        PortfolioProjectGroupSummary(
            project_group=project_groups.get(row.group_id),
            project_count=int(row.project_count),
            projects=projects_by_group.get(row.group_id, []),
        )
        for row in group_rows
    ]
    return groups, total_groups


def _uuid_values(values: set[UUID]) -> list[str]:
    return [str(value) for value in sorted(values, key=str)]


def _project_filters(
    *,
    accessible_ids: list[str] | None,
    status: ProjectStatus | None,
) -> list[Any]:
    filters: list[Any] = []
    if accessible_ids is not None:
        filters.append(ProjectModel.project_id.in_(accessible_ids))
    if status is not None:
        filters.append(ProjectModel.status == status.value)
    return filters


def _page_visible_groups(
    session: OrmSession,
    *,
    project_filters: list[Any],
    limit: int,
    offset: int,
) -> tuple[list[Any], int]:
    ranked_projects = (
        select(
            ProjectModel.group_id.label("group_id"),
            ProjectModel.created_at.label("first_created_at"),
            ProjectModel.project_id.label("first_project_id"),
            func.count(ProjectModel.project_id)
            .over(partition_by=ProjectModel.group_id)
            .label("project_count"),
            func.row_number()
            .over(
                partition_by=ProjectModel.group_id,
                order_by=(ProjectModel.created_at, ProjectModel.project_id),
            )
            .label("group_rank"),
        )
        .where(*project_filters)
        .subquery()
    )
    groups_stmt = (
        select(
            ranked_projects.c.group_id,
            ranked_projects.c.project_count,
            ranked_projects.c.first_created_at,
            ranked_projects.c.first_project_id,
        )
        .where(ranked_projects.c.group_rank == 1)
        .order_by(ranked_projects.c.first_created_at, ranked_projects.c.first_project_id)
    )
    total = count_from_statement(session, groups_stmt)
    rows = list(session.execute(apply_pagination(groups_stmt, limit=limit, offset=offset)))
    return rows, total


def _projects_for_groups(
    session: OrmSession,
    *,
    project_filters: list[Any],
    group_ids: list[str | None],
) -> list[ProjectModel]:
    group_filter = _group_filter(group_ids)
    stmt = (
        select(ProjectModel)
        .where(*project_filters, group_filter)
        .order_by(ProjectModel.created_at, ProjectModel.project_id)
    )
    return list(session.scalars(stmt))


def _group_filter(group_ids: list[str | None]) -> Any:
    non_null_group_ids = [group_id for group_id in group_ids if group_id is not None]
    clauses: list[Any] = []
    if non_null_group_ids:
        clauses.append(ProjectModel.group_id.in_(non_null_group_ids))
    if any(group_id is None for group_id in group_ids):
        clauses.append(ProjectModel.group_id.is_(None))
    return or_(*clauses)


def _project_groups_by_id(
    session: OrmSession,
    group_ids: Iterable[str],
) -> dict[str, ProjectGroup]:
    group_id_values = sorted(set(group_ids))
    if not group_id_values:
        return {}
    rows = list(
        session.scalars(
            select(ProjectGroupModel).where(ProjectGroupModel.group_id.in_(group_id_values))
        )
    )
    return {row.group_id: project_group_from_model(row) for row in rows}


def _summary_by_project(
    session: OrmSession,
    project_rows: list[ProjectModel],
) -> dict[str, PortfolioProjectSummary]:
    project_ids = [row.project_id for row in project_rows]
    open_question_counts = _count_by_project(
        session,
        QuestionModel,
        project_ids,
        statuses={QuestionStatus.STAGED.value, QuestionStatus.ACTIVE.value},
    )
    draft_dataset_counts = _count_by_project(
        session,
        DatasetModel,
        project_ids,
        statuses={DatasetStatus.STAGED.value},
    )
    committed_dataset_counts = _count_by_project(
        session,
        DatasetModel,
        project_ids,
        statuses={DatasetStatus.COMMITTED.value},
    )
    staged_analysis_counts = _count_by_project(
        session,
        AnalysisModel,
        project_ids,
        statuses={AnalysisStatus.STAGED.value},
    )
    unreviewed_claim_counts = _count_by_project(
        session,
        ClaimModel,
        project_ids,
        statuses={ClaimStatus.PROPOSED.value},
    )
    last_activity_at = _last_activity_by_project(session, project_rows)
    owners = _owners_by_project(session, project_ids)
    dataset_gap_counts = _dataset_gap_counts(session, project_ids)
    analysis_gap_counts = _analysis_gap_counts(session, project_ids)
    overdue_goal_counts = _overdue_goal_counts(session, project_ids)

    summaries: dict[str, PortfolioProjectSummary] = {}
    for project in project_rows:
        project_id = project.project_id
        open_question_count = open_question_counts[project_id]
        unreviewed_claim_count = unreviewed_claim_counts[project_id]
        activity_at = last_activity_at.get(project_id)
        summaries[project_id] = PortfolioProjectSummary(
            project_id=UUID(project_id),
            name=project.name,
            status=ProjectStatus(project.status),
            open_question_count=open_question_count,
            draft_dataset_count=draft_dataset_counts[project_id],
            committed_dataset_count=committed_dataset_counts[project_id],
            staged_analysis_count=staged_analysis_counts[project_id],
            unreviewed_claim_count=unreviewed_claim_count,
            last_activity_at=activity_at,
            owners=owners.get(project_id, []),
            triage_flags=_triage_flags(
                open_question_count=open_question_count,
                unreviewed_claim_count=unreviewed_claim_count,
                last_activity_at=activity_at,
                dataset_gap_count=dataset_gap_counts[project_id],
                analysis_gap_count=analysis_gap_counts[project_id],
                overdue_goal_count=overdue_goal_counts[project_id],
            ),
        )
    return summaries


def _count_by_project(
    session: OrmSession,
    model_type: Any,
    project_ids: list[str],
    *,
    statuses: set[str],
) -> defaultdict[str, int]:
    counts: defaultdict[str, int] = defaultdict(int)
    if not project_ids:
        return counts
    rows = session.execute(
        select(model_type.project_id, func.count())
        .where(model_type.project_id.in_(project_ids), model_type.status.in_(sorted(statuses)))
        .group_by(model_type.project_id)
    )
    for project_id, count in rows:
        counts[project_id] = int(count)
    return counts


def _owners_by_project(
    session: OrmSession,
    project_ids: list[str],
) -> dict[str, list[PortfolioProjectOwner]]:
    if not project_ids:
        return {}
    rows = session.execute(
        select(
            ProjectMembershipModel.project_id,
            ProjectMembershipModel.user_id,
            UserModel.username,
        )
        .join(UserModel, UserModel.user_id == ProjectMembershipModel.user_id)
        .where(
            ProjectMembershipModel.project_id.in_(project_ids),
            ProjectMembershipModel.role == ProjectMembershipRole.OWNER.value,
        )
        .order_by(ProjectMembershipModel.project_id, UserModel.username)
    )
    owners: dict[str, list[PortfolioProjectOwner]] = defaultdict(list)
    for project_id, user_id, username in rows:
        owners[project_id].append(
            PortfolioProjectOwner(user_id=UUID(user_id), username=username)
        )
    return owners


def _last_activity_by_project(
    session: OrmSession,
    project_rows: list[ProjectModel],
) -> dict[str, datetime]:
    project_ids = [row.project_id for row in project_rows]
    activity = {
        row.project_id: _latest_timestamp(row.created_at, row.updated_at)
        for row in project_rows
    }
    _merge_activity(
        session,
        activity,
        model_type=QuestionModel,
        project_ids=project_ids,
        timestamp_columns=(QuestionModel.created_at, QuestionModel.updated_at),
    )
    _merge_activity(
        session,
        activity,
        model_type=DatasetModel,
        project_ids=project_ids,
        timestamp_columns=(DatasetModel.created_at, DatasetModel.updated_at),
    )
    _merge_activity(
        session,
        activity,
        model_type=NoteModel,
        project_ids=project_ids,
        timestamp_columns=(NoteModel.created_at, NoteModel.updated_at),
    )
    _merge_activity(
        session,
        activity,
        model_type=SessionModel,
        project_ids=project_ids,
        timestamp_columns=(SessionModel.updated_at,),
    )
    _merge_activity(
        session,
        activity,
        model_type=AnalysisModel,
        project_ids=project_ids,
        timestamp_columns=(AnalysisModel.created_at, AnalysisModel.updated_at),
    )
    _merge_activity(
        session,
        activity,
        model_type=ClaimModel,
        project_ids=project_ids,
        timestamp_columns=(ClaimModel.created_at, ClaimModel.updated_at),
    )
    _merge_goal_activity(session, activity, project_ids)
    _merge_visualization_activity(session, activity, project_ids)
    _merge_activity(
        session,
        activity,
        model_type=GraphChangeSetModel,
        project_ids=project_ids,
        timestamp_columns=(
            GraphChangeSetModel.created_at,
            GraphChangeSetModel.updated_at,
        ),
    )
    return activity


def _merge_activity(
    session: OrmSession,
    activity: dict[str, datetime],
    *,
    model_type: Any,
    project_ids: list[str],
    timestamp_columns: tuple[Any, ...],
) -> None:
    if not project_ids:
        return
    timestamp_labels = [
        func.max(column).label(f"timestamp_{index}")
        for index, column in enumerate(timestamp_columns)
    ]
    rows = session.execute(
        select(model_type.project_id, *timestamp_labels)
        .where(model_type.project_id.in_(project_ids))
        .group_by(model_type.project_id)
    )
    for row in rows:
        _set_latest(activity, row[0], *row[1:])


def _merge_goal_activity(
    session: OrmSession,
    activity: dict[str, datetime],
    project_ids: list[str],
) -> None:
    if not project_ids:
        return
    direct_rows = session.execute(
        select(
            GoalModel.project_id,
            func.max(GoalModel.created_at),
            func.max(GoalModel.updated_at),
        )
        .where(GoalModel.project_id.in_(project_ids))
        .group_by(GoalModel.project_id)
    )
    for project_id, created_at, updated_at in direct_rows:
        _set_latest(activity, project_id, created_at, updated_at)

    linked_rows = session.execute(
        select(
            GoalLinkModel.entity_id,
            func.max(GoalModel.created_at),
            func.max(GoalModel.updated_at),
        )
        .join(GoalModel, GoalModel.goal_id == GoalLinkModel.goal_id)
        .where(
            GoalModel.project_id.is_(None),
            GoalLinkModel.entity_type == EntityType.PROJECT.value,
            GoalLinkModel.entity_id.in_(project_ids),
        )
        .group_by(GoalLinkModel.entity_id)
    )
    for project_id, created_at, updated_at in linked_rows:
        _set_latest(activity, project_id, created_at, updated_at)


def _merge_visualization_activity(
    session: OrmSession,
    activity: dict[str, datetime],
    project_ids: list[str],
) -> None:
    if not project_ids:
        return
    rows = session.execute(
        select(
            AnalysisModel.project_id,
            func.max(VisualizationModel.created_at),
            func.max(VisualizationModel.updated_at),
        )
        .join(AnalysisModel, AnalysisModel.analysis_id == VisualizationModel.analysis_id)
        .where(AnalysisModel.project_id.in_(project_ids))
        .group_by(AnalysisModel.project_id)
    )
    for project_id, created_at, updated_at in rows:
        _set_latest(activity, project_id, created_at, updated_at)


def _latest_timestamp(*values: datetime | None) -> datetime:
    candidates = [as_utc(value) for value in values if value is not None]
    return max(candidates)


def _set_latest(
    activity: dict[str, datetime],
    project_id: str | None,
    *values: datetime | None,
) -> None:
    if project_id is None:
        return
    candidates = [as_utc(value) for value in values if value is not None]
    if not candidates:
        return
    latest = max(candidates)
    current = activity.get(project_id)
    if current is None or latest > current:
        activity[project_id] = latest


def _dataset_gap_counts(
    session: OrmSession,
    project_ids: list[str],
) -> defaultdict[str, int]:
    counts: defaultdict[str, int] = defaultdict(int)
    if not project_ids:
        return counts
    active_analysis_exists = (
        select(literal(1))
        .select_from(AnalysisDatasetModel)
        .join(AnalysisModel, AnalysisModel.analysis_id == AnalysisDatasetModel.analysis_id)
        .where(
            AnalysisDatasetModel.dataset_id == DatasetModel.dataset_id,
            AnalysisModel.project_id == DatasetModel.project_id,
            AnalysisModel.status != AnalysisStatus.ARCHIVED.value,
        )
        .exists()
    )
    rows = session.execute(
        select(DatasetModel.project_id, func.count(DatasetModel.dataset_id))
        .where(
            DatasetModel.project_id.in_(project_ids),
            DatasetModel.status == DatasetStatus.COMMITTED.value,
            ~active_analysis_exists,
        )
        .group_by(DatasetModel.project_id)
    )
    for project_id, count in rows:
        counts[project_id] = int(count)
    return counts


def _analysis_gap_counts(
    session: OrmSession,
    project_ids: list[str],
) -> defaultdict[str, int]:
    counts: defaultdict[str, int] = defaultdict(int)
    if not project_ids:
        return counts
    active_claim_exists = (
        select(literal(1))
        .select_from(ClaimAnalysisModel)
        .join(ClaimModel, ClaimModel.claim_id == ClaimAnalysisModel.claim_id)
        .where(
            ClaimAnalysisModel.analysis_id == AnalysisModel.analysis_id,
            ClaimModel.project_id == AnalysisModel.project_id,
            ClaimModel.status != ClaimStatus.REJECTED.value,
        )
        .exists()
    )
    rows = session.execute(
        select(AnalysisModel.project_id, func.count(AnalysisModel.analysis_id))
        .where(
            AnalysisModel.project_id.in_(project_ids),
            AnalysisModel.status == AnalysisStatus.COMMITTED.value,
            ~active_claim_exists,
        )
        .group_by(AnalysisModel.project_id)
    )
    for project_id, count in rows:
        counts[project_id] = int(count)
    return counts


def _overdue_goal_counts(
    session: OrmSession,
    project_ids: list[str],
) -> defaultdict[str, int]:
    counts: defaultdict[str, int] = defaultdict(int)
    if not project_ids:
        return counts
    today = datetime.now(timezone.utc).date()
    direct_rows = session.execute(
        select(GoalModel.project_id, func.count(GoalModel.goal_id))
        .where(
            GoalModel.project_id.in_(project_ids),
            GoalModel.target_date.is_not(None),
            GoalModel.target_date < today,
            GoalModel.status.in_(sorted(OPEN_GOAL_STATUSES)),
        )
        .group_by(GoalModel.project_id)
    )
    for project_id, count in direct_rows:
        counts[project_id] += int(count)

    linked_rows = session.execute(
        select(GoalLinkModel.entity_id, func.count(func.distinct(GoalModel.goal_id)))
        .join(GoalModel, GoalModel.goal_id == GoalLinkModel.goal_id)
        .where(
            GoalModel.project_id.is_(None),
            GoalModel.target_date.is_not(None),
            GoalModel.target_date < today,
            GoalModel.status.in_(sorted(OPEN_GOAL_STATUSES)),
            GoalLinkModel.entity_type == EntityType.PROJECT.value,
            GoalLinkModel.entity_id.in_(project_ids),
        )
        .group_by(GoalLinkModel.entity_id)
    )
    for project_id, count in linked_rows:
        counts[project_id] += int(count)
    return counts


def _triage_flags(
    *,
    open_question_count: int,
    unreviewed_claim_count: int,
    last_activity_at: datetime | None,
    dataset_gap_count: int,
    analysis_gap_count: int,
    overdue_goal_count: int,
) -> list[PortfolioTriageFlag]:
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
    if dataset_gap_count:
        flags.append(
            PortfolioTriageFlag(
                key="datasets_without_analyses",
                label="Datasets without analyses",
                count=dataset_gap_count,
            )
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
    stale_after = datetime.now(timezone.utc) - STALE_PROJECT_AFTER
    return as_utc(last_activity_at) < stale_after
