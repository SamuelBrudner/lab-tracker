"""Graph draft review routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.config import get_settings
from lab_tracker.graph_drafting import OpenAIGraphDraftClient
from lab_tracker.models import GraphChangeSet, GraphChangeSetStatus
from lab_tracker.schemas import (
    Envelope,
    GraphDraftCommitRequest,
    GraphDraftOperationUpdate,
    ListEnvelope,
)

from .shared import (
    actor_from_request,
    api_from_request,
    list_response,
    paginate,
    validate_pagination,
)


def build_graph_drafts_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/notes/{note_id:uuid}/graph-drafts",
        response_model=Envelope[GraphChangeSet],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_graph_draft(note_id: UUID, request: Request):
        actor = actor_from_request(request)
        draft_client = _draft_client_from_request(request)
        try:
            change_set = api_from_request(request, api).create_graph_draft_from_note(
                note_id,
                draft_client=draft_client,
                actor=actor,
            )
        finally:
            close = getattr(draft_client, "close", None)
            if callable(close):
                close()
        return Envelope(data=change_set)

    @router.get("/graph-drafts", response_model=ListEnvelope[GraphChangeSet])
    def list_graph_drafts(
        request: Request,
        project_id: UUID | None = None,
        status: GraphChangeSetStatus | None = None,
        source_note_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        change_sets = api_from_request(request, api).list_graph_change_sets(
            project_id=project_id,
            status=status,
            source_note_id=source_note_id,
        )
        items, total = paginate(change_sets, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.get("/graph-drafts/{change_set_id:uuid}", response_model=Envelope[GraphChangeSet])
    def get_graph_draft(change_set_id: UUID, request: Request):
        change_set = api_from_request(request, api).get_graph_change_set(change_set_id)
        return Envelope(data=change_set)

    @router.patch(
        "/graph-drafts/{change_set_id:uuid}/operations/{operation_id:uuid}",
        response_model=Envelope[GraphChangeSet],
    )
    def update_graph_draft_operation(
        change_set_id: UUID,
        operation_id: UUID,
        payload: GraphDraftOperationUpdate,
        request: Request,
    ):
        actor = actor_from_request(request)
        change_set = api_from_request(request, api).update_graph_change_operation(
            change_set_id,
            operation_id,
            payload=payload.payload,
            status=payload.status,
            actor=actor,
        )
        return Envelope(data=change_set)

    @router.post(
        "/graph-drafts/{change_set_id:uuid}/commit",
        response_model=Envelope[GraphChangeSet],
    )
    def commit_graph_draft(
        change_set_id: UUID,
        payload: GraphDraftCommitRequest,
        request: Request,
    ):
        actor = actor_from_request(request)
        change_set = api_from_request(request, api).commit_graph_change_set(
            change_set_id,
            message=payload.message,
            actor=actor,
        )
        return Envelope(data=change_set)

    return router


def _draft_client_from_request(request: Request):
    settings = getattr(request.app.state, "settings", None) or get_settings()
    factory = getattr(request.app.state, "graph_draft_client_factory", None)
    if callable(factory):
        return factory(settings)
    return OpenAIGraphDraftClient.from_settings(settings)
