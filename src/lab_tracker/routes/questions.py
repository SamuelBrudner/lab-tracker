"""Question routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import Question, QuestionRefactor, QuestionStatus, QuestionType
from lab_tracker.schemas import (
    Envelope,
    ListEnvelope,
    QuestionCreate,
    QuestionRefactorRequest,
    QuestionRefactorResult,
    QuestionUpdate,
)

from .shared import (
    actor_from_request,
    api_from_request,
    ensure_project_read,
    filter_project_scoped_items,
    list_response,
    paginate,
    question_default_status,
    repository_from_request,
    validate_pagination,
)


def build_questions_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/questions",
        response_model=Envelope[Question],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_question(payload: QuestionCreate, request: Request):
        actor = actor_from_request(request)
        question = api_from_request(request, api).create_question(
            project_id=payload.project_id,
            text=payload.text,
            question_type=payload.question_type,
            hypothesis=payload.hypothesis,
            status=payload.status or question_default_status(),
            parent_question_ids=payload.parent_question_ids,
            actor=actor,
        )
        return Envelope(data=question)

    @router.get("/questions", response_model=ListEnvelope[Question])
    def list_questions(
        request: Request,
        project_id: UUID | None = None,
        status: QuestionStatus | None = None,
        question_type: QuestionType | None = None,
        search: str | None = None,
        q: str | None = None,
        created_by: str | None = None,
        parent_question_id: UUID | None = None,
        ancestor_question_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        resolved_search = search or q
        if project_id is not None:
            ensure_project_read(request, project_id)
        questions, _ = repository_from_request(request).query_questions(
            project_id=project_id,
            status=status.value if status is not None else None,
            question_type=question_type.value if question_type is not None else None,
            search=resolved_search,
            created_by=created_by,
            parent_question_id=parent_question_id,
            ancestor_question_id=ancestor_question_id,
            limit=None,
            offset=0,
        )
        visible = filter_project_scoped_items(request, questions)
        items, total = paginate(visible, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.get("/questions/{question_id}", response_model=Envelope[Question])
    def get_question(question_id: UUID, request: Request):
        question = api_from_request(request, api).get_question(question_id)
        ensure_project_read(request, question.project_id)
        return Envelope(data=question)

    @router.patch("/questions/{question_id}", response_model=Envelope[Question])
    def update_question(question_id: UUID, payload: QuestionUpdate, request: Request):
        actor = actor_from_request(request)
        existing = api_from_request(request, api).get_question(question_id)
        ensure_project_read(request, existing.project_id)
        question = api_from_request(request, api).update_question(
            question_id,
            text=payload.text,
            question_type=payload.question_type,
            hypothesis=payload.hypothesis,
            status=payload.status,
            parent_question_ids=payload.parent_question_ids,
            actor=actor,
        )
        return Envelope(data=question)

    @router.post(
        "/questions/{question_id}/refactor",
        response_model=Envelope[QuestionRefactorResult],
        status_code=http_status.HTTP_201_CREATED,
    )
    def refactor_question(question_id: UUID, payload: QuestionRefactorRequest, request: Request):
        actor = actor_from_request(request)
        source = api_from_request(request, api).get_question(question_id)
        ensure_project_read(request, source.project_id)
        result = api_from_request(request, api).refactor_question(
            question_id,
            replacement_text=payload.replacement.text,
            replacement_question_type=payload.replacement.question_type,
            replacement_hypothesis=payload.replacement.hypothesis,
            replacement_status=payload.replacement.status,
            replacement_parent_question_ids=payload.replacement.parent_question_ids,
            reason=payload.reason,
            child_question_ids_to_reparent=payload.child_question_ids_to_reparent,
            note_ids_to_retarget=payload.note_ids_to_retarget,
            actor=actor,
        )
        return Envelope(
            data=QuestionRefactorResult(
                source_question=result.source_question,
                replacement_question=result.replacement_question,
                refactor=result.refactor,
            )
        )

    @router.get(
        "/questions/{question_id}/refactors",
        response_model=ListEnvelope[QuestionRefactor],
    )
    def list_question_refactors(
        question_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        question = api_from_request(request, api).get_question(question_id)
        ensure_project_read(request, question.project_id)
        refactors = api_from_request(request, api).list_question_refactors(
            question_id,
            limit=limit,
            offset=offset,
        )
        total = len(
            api_from_request(request, api).list_question_refactors(
                question_id,
                limit=None,
                offset=0,
            )
        )
        return list_response(refactors, limit=limit, offset=offset, total=total)

    @router.delete("/questions/{question_id}", response_model=Envelope[Question])
    def delete_question(question_id: UUID, request: Request):
        actor = actor_from_request(request)
        existing = api_from_request(request, api).get_question(question_id)
        ensure_project_read(request, existing.project_id)
        question = api_from_request(request, api).delete_question(question_id, actor=actor)
        return Envelope(data=question)

    return router
