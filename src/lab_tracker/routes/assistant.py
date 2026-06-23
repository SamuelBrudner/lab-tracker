"""Assistant-facing read-only context routes."""

from __future__ import annotations

from fastapi import APIRouter
from starlette.requests import Request

from lab_tracker.decision_context import (
    JsonObject,
    RepositoryDecisionContextReader,
    build_decision_context,
)
from lab_tracker.schemas import AssistantDecisionContextRequest

from .shared import (
    accessible_project_ids_from_request,
    ensure_project_read,
    repository_from_request,
)


def build_assistant_router() -> APIRouter:
    router = APIRouter()

    @router.post("/assistant/decision-context")
    def get_decision_context(
        payload: AssistantDecisionContextRequest,
        request: Request,
    ) -> JsonObject:
        """Build bounded graph context before research-facing assistant decisions."""
        accessible_project_ids = accessible_project_ids_from_request(request)
        resolved_project_id = payload.project_id
        if resolved_project_id is not None:
            ensure_project_read(request, resolved_project_id)
        elif accessible_project_ids is not None and len(accessible_project_ids) == 1:
            resolved_project_id = next(iter(accessible_project_ids))
        reader = RepositoryDecisionContextReader(
            repository_from_request(request),
            accessible_project_ids=accessible_project_ids,
        )
        return build_decision_context(
            reader,
            task_kind=payload.task_kind,
            query=payload.query,
            project_id=str(resolved_project_id) if resolved_project_id else None,
            question_id=str(payload.question_id) if payload.question_id else None,
            dataset_id=str(payload.dataset_id) if payload.dataset_id else None,
            analysis_id=str(payload.analysis_id) if payload.analysis_id else None,
            claim_id=str(payload.claim_id) if payload.claim_id else None,
            visualization_id=(
                str(payload.visualization_id) if payload.visualization_id else None
            ),
            created_by=str(payload.created_by) if payload.created_by else None,
            since=payload.since,
            until=payload.until,
            limit=payload.limit,
        )

    return router
