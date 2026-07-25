"""Search routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.schemas import Envelope, SearchResults

from .shared import (
    actor_from_request,
    handlers_from_request,
    validate_pagination,
)


def build_search_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/search", response_model=Envelope[SearchResults])
    def search(
        request: Request,
        q: str,
        project_id: UUID | None = None,
        goal_id: UUID | None = None,
        include: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        results = handlers_from_request(request).context.search(
            actor=actor_from_request(request),
            query=q,
            project_id=project_id,
            goal_id=goal_id,
            include=include,
            limit=limit,
            offset=offset,
        )
        return Envelope(
            data=results,
            meta={
                "questions_count": len(results.questions),
                "notes_count": len(results.notes),
                "experiments_count": len(results.experiments),
            },
        )

    return router
