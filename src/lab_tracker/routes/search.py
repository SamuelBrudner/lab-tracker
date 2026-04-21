"""Search routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.schemas import Envelope, SearchResults

from .shared import api_from_request, validate_pagination


def build_search_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/search", response_model=Envelope[SearchResults])
    def search(
        request: Request,
        q: str,
        project_id: UUID | None = None,
        include: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        request_api = api_from_request(request, api)
        include_set = {
            item.strip().casefold()
            for item in (include.split(",") if include else ["questions", "notes"])
            if item.strip()
        }
        questions = (
            request_api.search_questions(q, project_id=project_id, limit=limit, offset=offset)
            if not include_set or "questions" in include_set
            else []
        )
        notes = (
            request_api.search_notes(q, project_id=project_id, limit=limit, offset=offset)
            if not include_set or "notes" in include_set
            else []
        )
        return Envelope(
            data=SearchResults(questions=questions, notes=notes),
            meta={"questions_count": len(questions), "notes_count": len(notes)},
        )

    return router
