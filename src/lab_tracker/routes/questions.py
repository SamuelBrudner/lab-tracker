"""Question routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request
from starlette.responses import Response

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import (
    EntityType,
    EntityVersion,
    EntityVersionDiff,
    Question,
    QuestionRefactor,
    QuestionStatus,
    QuestionType,
    UsageEventResourceType,
)
from lab_tracker.schemas import (
    Envelope,
    ListEnvelope,
    QuestionCreate,
    QuestionRefactorRequest,
    QuestionRefactorResult,
    QuestionUpdate,
)

from .shared import (
    CreatedByFilter,
    accessible_project_ids_from_request,
    actor_from_request,
    api_from_request,
    ensure_project_read,
    list_response,
    paginate,
    question_default_status,
    record_usage_view,
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
    def create_question(payload: QuestionCreate, request: Request, response: Response):
        actor = actor_from_request(request)
        result = api_from_request(request, api).create_question_result(
            project_id=payload.project_id,
            text=payload.text,
            question_type=payload.question_type,
            hypothesis=payload.hypothesis,
            status=payload.status or question_default_status(),
            client_capture_id=payload.client_capture_id,
            terminal_reason=payload.terminal_reason,
            parent_question_ids=payload.parent_question_ids,
            actor=actor,
        )
        if result.reused:
            response.status_code = http_status.HTTP_200_OK
        return Envelope(data=result.entity)

    @router.get("/questions", response_model=ListEnvelope[Question])
    def list_questions(
        request: Request,
        project_id: UUID | None = None,
        status: QuestionStatus | None = None,
        question_type: QuestionType | None = None,
        search: str | None = None,
        q: str | None = None,
        created_by: CreatedByFilter = None,
        parent_question_id: UUID | None = None,
        ancestor_question_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        resolved_search = search or q
        if project_id is not None:
            ensure_project_read(request, project_id)
            project_ids = None
        else:
            project_ids = accessible_project_ids_from_request(request)
        questions, total = repository_from_request(request).query_questions(
            project_id=project_id,
            project_ids=project_ids,
            status=status.value if status is not None else None,
            question_type=question_type.value if question_type is not None else None,
            search=resolved_search,
            created_by=created_by,
            parent_question_id=parent_question_id,
            ancestor_question_id=ancestor_question_id,
            limit=limit,
            offset=offset,
        )
        return list_response(questions, limit=limit, offset=offset, total=total)

    @router.get("/questions/{question_id}", response_model=Envelope[Question])
    def get_question(question_id: UUID, request: Request):
        question = api_from_request(request, api).get_question(question_id)
        ensure_project_read(request, question.project_id)
        record_usage_view(
            request,
            resource_type=UsageEventResourceType.QUESTION,
            resource_id=question.question_id,
            project_id=question.project_id,
        )
        return Envelope(data=question)

    @router.get("/questions/{question_id}/versions", response_model=ListEnvelope[EntityVersion])
    def list_question_versions(
        question_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        question = api_from_request(request, api).get_question(question_id)
        ensure_project_read(request, question.project_id)
        versions = api_from_request(request, api).list_entity_versions(
            entity_type=EntityType.QUESTION,
            entity_id=question_id,
        )
        items, total = paginate(versions, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.get(
        "/questions/{question_id}/versions/diff",
        response_model=Envelope[EntityVersionDiff],
    )
    def diff_question_versions(
        question_id: UUID,
        request: Request,
        from_version: int,
        to_version: int,
    ):
        question = api_from_request(request, api).get_question(question_id)
        ensure_project_read(request, question.project_id)
        diff = api_from_request(request, api).diff_entity_versions(
            entity_type=EntityType.QUESTION,
            entity_id=question_id,
            from_version=from_version,
            to_version=to_version,
        )
        return Envelope(data=diff)

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
            terminal_reason=payload.terminal_reason,
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
