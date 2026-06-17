"""Portfolio summary routes."""

from __future__ import annotations

from fastapi import APIRouter
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import ProjectStatus
from lab_tracker.portfolio_query import portfolio_summary_groups
from lab_tracker.schemas import (
    ListEnvelope,
    PortfolioProjectGroupSummary,
)

from .shared import (
    accessible_project_ids_from_request,
    db_session_from_request,
    list_response,
    validate_pagination,
)


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
        accessible_project_ids = accessible_project_ids_from_request(request)
        groups, total = portfolio_summary_groups(
            db_session_from_request(request),
            accessible_project_ids=accessible_project_ids,
            status=status,
            limit=limit,
            offset=offset,
        )
        return list_response(groups, limit=limit, offset=offset, total=total)

    return router
