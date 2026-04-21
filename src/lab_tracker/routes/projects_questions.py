"""Project and question routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import (
    Project,
    ProjectReviewPolicy,
    ProjectStatus,
    Question,
    QuestionSource,
    QuestionStatus,
    QuestionType,
)
from lab_tracker.schemas import (
    Envelope,
    ListEnvelope,
    ProjectCreate,
    ProjectUpdate,
    QuestionCreate,
    QuestionUpdate,
)

from .shared import (
    actor_from_request,
    list_response,
    paginate,
    project_default_status,
    question_default_status,
    repository_from_request,
    validate_pagination,
)


def build_projects_questions_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/projects",
        response_model=Envelope[Project],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_project(payload: ProjectCreate, request: Request):
        actor = actor_from_request(request)
        project = api.create_project(
            name=payload.name,
            description=payload.description or "",
            status=payload.status or project_default_status(),
            review_policy=payload.review_policy or ProjectReviewPolicy.NONE,
            actor=actor,
        )
        return Envelope(data=project)

    @router.get("/projects", response_model=ListEnvelope[Project])
    def list_projects(
        request: Request,
        status: ProjectStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        projects, total = repository_from_request(request).query_projects(
            status=status.value if status is not None else None,
            limit=limit,
            offset=offset,
        )
        return list_response(projects, limit=limit, offset=offset, total=total)

    @router.get("/projects/{project_id}", response_model=Envelope[Project])
    def get_project(project_id: UUID):
        project = api.get_project(project_id)
        return Envelope(data=project)

    @router.patch("/projects/{project_id}", response_model=Envelope[Project])
    def update_project(project_id: UUID, payload: ProjectUpdate, request: Request):
        actor = actor_from_request(request)
        project = api.update_project(
            project_id,
            name=payload.name,
            description=payload.description,
            status=payload.status,
            review_policy=payload.review_policy,
            actor=actor,
        )
        return Envelope(data=project)

    @router.delete("/projects/{project_id}", response_model=Envelope[Project])
    def delete_project(project_id: UUID, request: Request):
        actor = actor_from_request(request)
        project = api.delete_project(project_id, actor=actor)
        return Envelope(data=project)

    @router.post(
        "/questions",
        response_model=Envelope[Question],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_question(payload: QuestionCreate, request: Request):
        actor = actor_from_request(request)
        question = api.create_question(
            project_id=payload.project_id,
            text=payload.text,
            question_type=payload.question_type,
            hypothesis=payload.hypothesis,
            status=payload.status or question_default_status(),
            parent_question_ids=payload.parent_question_ids,
            created_from=payload.created_from or QuestionSource.MANUAL,
            source_provenance=payload.source_provenance,
            actor=actor,
        )
        return Envelope(data=question)

    @router.get("/questions", response_model=ListEnvelope[Question])
    def list_questions(
        request: Request,
        project_id: UUID | None = None,
        status: QuestionStatus | None = None,
        question_type: QuestionType | None = None,
        created_from: QuestionSource | None = None,
        search: str | None = None,
        q: str | None = None,
        parent_question_id: UUID | None = None,
        ancestor_question_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        resolved_search = search or q
        if not resolved_search:
            questions, total = repository_from_request(request).query_questions(
                project_id=project_id,
                status=status.value if status is not None else None,
                question_type=question_type.value if question_type is not None else None,
                created_from=created_from.value if created_from is not None else None,
                parent_question_id=parent_question_id,
                ancestor_question_id=ancestor_question_id,
                limit=limit,
                offset=offset,
            )
            return list_response(questions, limit=limit, offset=offset, total=total)
        questions = api.list_questions(
            project_id=project_id,
            status=status,
            question_type=question_type,
            created_from=created_from,
            search=resolved_search,
            parent_question_id=parent_question_id,
            ancestor_question_id=ancestor_question_id,
        )
        page, total = paginate(questions, limit, offset)
        return list_response(page, limit=limit, offset=offset, total=total)

    @router.get("/questions/{question_id}", response_model=Envelope[Question])
    def get_question(question_id: UUID):
        question = api.get_question(question_id)
        return Envelope(data=question)

    @router.patch("/questions/{question_id}", response_model=Envelope[Question])
    def update_question(question_id: UUID, payload: QuestionUpdate, request: Request):
        actor = actor_from_request(request)
        question = api.update_question(
            question_id,
            text=payload.text,
            question_type=payload.question_type,
            hypothesis=payload.hypothesis,
            status=payload.status,
            parent_question_ids=payload.parent_question_ids,
            actor=actor,
        )
        return Envelope(data=question)

    @router.delete("/questions/{question_id}", response_model=Envelope[Question])
    def delete_question(question_id: UUID, request: Request):
        actor = actor_from_request(request)
        question = api.delete_question(question_id, actor=actor)
        return Envelope(data=question)

    return router
