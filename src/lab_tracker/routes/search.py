"""Search routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.errors import ValidationError
from lab_tracker.models import EntityType, UsageEventResourceType, UsageEventVerb
from lab_tracker.schemas import Envelope, SearchResults

from .shared import (
    accessible_project_ids_from_request,
    actor_from_request,
    api_from_request,
    ensure_project_read,
    repository_from_request,
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
        repository = repository_from_request(request)
        allowed_project_ids = accessible_project_ids_from_request(request)
        if project_id is not None:
            ensure_project_read(request, project_id)
        include_set = {
            item.strip().casefold()
            for item in (include.split(",") if include else ["questions", "notes"])
            if item.strip()
        }
        project_ids = {project_id} if project_id is not None else allowed_project_ids
        linked_question_ids: set[UUID] | None = None
        linked_note_ids: set[UUID] | None = None
        if goal_id is not None:
            goal = api_from_request(request, api).get_goal(goal_id)
            ensure_project_read(request, goal.project_id)
            if project_id is not None and goal.project_id != project_id:
                raise ValidationError("goal_id must belong to project_id.")
            project_ids = {goal.project_id}
            links, _ = repository.query_goal_links(goal_id=goal_id, limit=None, offset=0)
            linked_question_ids = {
                link.target.entity_id
                for link in links
                if link.target.entity_type == EntityType.QUESTION
            }
            linked_note_ids = {
                link.target.entity_id
                for link in links
                if link.target.entity_type == EntityType.NOTE
            }
        questions = (
            repository.query_questions(
                project_id=None,
                project_ids=project_ids,
                search=q,
                limit=None if linked_question_ids is not None else limit,
                offset=0 if linked_question_ids is not None else offset,
            )[0]
            if not include_set or "questions" in include_set
            else []
        )
        notes = (
            repository.query_notes(
                project_id=None,
                project_ids=project_ids,
                search=q,
                limit=None if linked_note_ids is not None else limit,
                offset=0 if linked_note_ids is not None else offset,
            )[0]
            if not include_set or "notes" in include_set
            else []
        )
        if linked_question_ids is not None:
            questions = [item for item in questions if item.question_id in linked_question_ids]
        if linked_note_ids is not None:
            notes = [item for item in notes if item.note_id in linked_note_ids]
        if linked_question_ids is not None:
            questions = questions[offset : offset + limit]
        if linked_note_ids is not None:
            notes = notes[offset : offset + limit]
        api_from_request(request, api).record_usage_event(
            verb=UsageEventVerb.SEARCH,
            resource_type=UsageEventResourceType.SEARCH,
            project_id=_single_project_id(project_ids),
            actor=actor_from_request(request),
            result_count=len(questions) + len(notes),
        )
        return Envelope(
            data=SearchResults(questions=questions, notes=notes),
            meta={"questions_count": len(questions), "notes_count": len(notes)},
        )

    return router


def _single_project_id(project_ids: set[UUID] | None) -> UUID | None:
    if project_ids is None or len(project_ids) != 1:
        return None
    return next(iter(project_ids))
