"""Search routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.schemas import Envelope, SearchResults

from .shared import repository_from_request, validate_pagination
from .shared import accessible_project_ids_from_request, ensure_project_read


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
        repository = repository_from_request(request)
        allowed_project_ids = accessible_project_ids_from_request(request)
        if project_id is not None:
            ensure_project_read(request, project_id)
        include_set = {
            item.strip().casefold()
            for item in (include.split(",") if include else ["questions", "notes"])
            if item.strip()
        }
        project_ids = [project_id] if project_id is not None else None
        if project_ids is None and allowed_project_ids is not None:
            project_ids = sorted(allowed_project_ids)
        if project_ids is None:
            questions = (
                repository.query_questions(project_id=None, search=q, limit=limit, offset=offset)[0]
                if not include_set or "questions" in include_set
                else []
            )
            notes = (
                repository.query_notes(project_id=None, search=q, limit=limit, offset=offset)[0]
                if not include_set or "notes" in include_set
                else []
            )
        else:
            questions = []
            notes = []
            for scoped_project_id in project_ids:
                if not include_set or "questions" in include_set:
                    questions.extend(
                        repository.query_questions(
                            project_id=scoped_project_id,
                            search=q,
                            limit=limit,
                            offset=0,
                        )[0]
                    )
                if not include_set or "notes" in include_set:
                    notes.extend(
                        repository.query_notes(
                            project_id=scoped_project_id,
                            search=q,
                            limit=limit,
                            offset=0,
                        )[0]
                    )
            questions = questions[offset : offset + limit]
            notes = notes[offset : offset + limit]
        return Envelope(
            data=SearchResults(questions=questions, notes=notes),
            meta={"questions_count": len(questions), "notes_count": len(notes)},
        )

    return router
