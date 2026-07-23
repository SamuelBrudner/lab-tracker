"""Portfolio summary routes."""

from __future__ import annotations

from fastapi import APIRouter
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import ProjectStatus
from lab_tracker.schemas import (
    ListEnvelope,
    PortfolioProjectGroupSummary,
)

from .shared import (
    actor_from_request,
    handlers_from_request,
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
        page = handlers_from_request(request).context.portfolio_summary(
            actor=actor_from_request(request),
            status=status,
            limit=limit,
            offset=offset,
        )
        return list_response(
            page.items,
            limit=limit,
            offset=offset,
            total=page.total,
        )

    return router
