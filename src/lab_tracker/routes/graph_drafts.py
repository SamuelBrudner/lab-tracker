"""Graph draft review routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.config import get_settings
from lab_tracker.graph_drafting import make_graph_draft_client
from lab_tracker.models import GraphChangeSet, GraphChangeSetStatus, UsageEventResourceType
from lab_tracker.schemas import (
    Envelope,
    GraphChangeSetSummary,
    GraphDraftCommitRequest,
    GraphDraftCreateRequest,
    GraphDraftOperationUpdate,
    GraphDraftReviewRequest,
    GraphDraftReviseRequest,
    ListEnvelope,
)

from .shared import (
    accessible_project_ids_from_request,
    actor_from_request,
    api_from_request,
    ensure_project_read,
    list_response,
    record_usage_view,
    validate_pagination,
)


def build_graph_drafts_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/notes/{note_id:uuid}/graph-drafts",
        response_model=Envelope[GraphChangeSet],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_graph_draft(
        note_id: UUID,
        request: Request,
        payload: GraphDraftCreateRequest | None = None,
    ):
        actor = actor_from_request(request)
        draft_client = _draft_client_from_request(request)
        draft_payload = payload or GraphDraftCreateRequest()
        try:
            change_set = api_from_request(request, api).create_graph_draft_from_note(
                note_id,
                draft_client=draft_client,
                mode=draft_payload.mode,
                user_hint=draft_payload.user_hint,
                actor=actor,
            )
        finally:
            close = getattr(draft_client, "close", None)
            if callable(close):
                close()
        return Envelope(data=_attach_graph_usernames(request, change_set))

    @router.post(
        "/notes/{note_id:uuid}/analysis-graph-drafts",
        response_model=Envelope[GraphChangeSet],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_analysis_graph_draft(note_id: UUID, request: Request):
        actor = actor_from_request(request)
        draft_client = _draft_client_from_request(request)
        try:
            change_set = api_from_request(request, api).create_analysis_graph_draft_from_note(
                note_id,
                draft_client=draft_client,
                actor=actor,
            )
        finally:
            close = getattr(draft_client, "close", None)
            if callable(close):
                close()
        return Envelope(data=_attach_graph_usernames(request, change_set))

    @router.get("/graph-drafts", response_model=ListEnvelope[GraphChangeSetSummary])
    def list_graph_drafts(
        request: Request,
        project_id: UUID | None = None,
        status: GraphChangeSetStatus | None = None,
        source_note_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        if project_id is not None:
            ensure_project_read(request, project_id)
            project_ids = None
        else:
            project_ids = accessible_project_ids_from_request(request)
        change_sets, total = api_from_request(request, api).query_graph_change_sets(
            project_id=project_id,
            project_ids=project_ids,
            status=status,
            source_note_id=source_note_id,
            limit=limit,
            offset=offset,
            include_operations=False,
        )
        return list_response(
            [
                _graph_change_set_summary(_attach_graph_usernames(request, item))
                for item in change_sets
            ],
            limit=limit,
            offset=offset,
            total=total,
        )

    @router.get("/graph-drafts/{change_set_id:uuid}", response_model=Envelope[GraphChangeSet])
    def get_graph_draft(change_set_id: UUID, request: Request):
        change_set = api_from_request(request, api).get_graph_change_set(change_set_id)
        ensure_project_read(request, change_set.project_id)
        record_usage_view(
            request,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            resource_id=change_set.change_set_id,
            project_id=change_set.project_id,
        )
        return Envelope(data=_attach_graph_usernames(request, change_set))

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
        change_set = api_from_request(request, api).get_graph_change_set(change_set_id)
        ensure_project_read(request, change_set.project_id)
        change_set = api_from_request(request, api).update_graph_change_operation(
            change_set_id,
            operation_id,
            payload=payload.payload,
            status=payload.status,
            review_note=payload.review_note,
            actor=actor,
        )
        return Envelope(data=_attach_graph_usernames(request, change_set))

    @router.post(
        "/graph-drafts/{change_set_id:uuid}/accept-all",
        response_model=Envelope[GraphChangeSet],
    )
    def accept_all_graph_draft_operations(change_set_id: UUID, request: Request):
        actor = actor_from_request(request)
        change_set = api_from_request(request, api).get_graph_change_set(change_set_id)
        ensure_project_read(request, change_set.project_id)
        change_set = api_from_request(request, api).bulk_accept_graph_change_operations(
            change_set_id,
            actor=actor,
        )
        return Envelope(data=_attach_graph_usernames(request, change_set))

    @router.post(
        "/graph-drafts/{change_set_id:uuid}/submit",
        response_model=Envelope[GraphChangeSet],
    )
    def submit_graph_draft(change_set_id: UUID, request: Request):
        actor = actor_from_request(request)
        change_set = api_from_request(request, api).submit_graph_change_set(
            change_set_id,
            actor=actor,
        )
        return Envelope(data=_attach_graph_usernames(request, change_set))

    @router.post(
        "/graph-drafts/{change_set_id:uuid}/review",
        response_model=Envelope[GraphChangeSet],
    )
    def review_graph_draft(
        change_set_id: UUID,
        payload: GraphDraftReviewRequest,
        request: Request,
    ):
        actor = actor_from_request(request)
        change_set = api_from_request(request, api).review_graph_change_set(
            change_set_id,
            status=payload.status,
            note=payload.note,
            actor=actor,
        )
        return Envelope(data=_attach_graph_usernames(request, change_set))

    @router.post(
        "/graph-drafts/{change_set_id:uuid}/revise",
        response_model=Envelope[GraphChangeSet],
    )
    def revise_graph_draft(
        change_set_id: UUID,
        payload: GraphDraftReviseRequest,
        request: Request,
    ):
        actor = actor_from_request(request)
        draft_client = _draft_client_from_request(request)
        try:
            change_set = api_from_request(request, api).revise_graph_change_set(
                change_set_id,
                feedback=payload.feedback,
                draft_client=draft_client,
                actor=actor,
            )
        finally:
            close = getattr(draft_client, "close", None)
            if callable(close):
                close()
        return Envelope(data=_attach_graph_usernames(request, change_set))

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
        return Envelope(data=_attach_graph_usernames(request, change_set))

    return router


def _draft_client_from_request(request: Request):
    settings = getattr(request.app.state, "settings", None) or get_settings()
    factory = getattr(request.app.state, "graph_draft_client_factory", None)
    if callable(factory):
        return factory(settings)
    return make_graph_draft_client(settings)


def _attach_graph_usernames(request: Request, change_set: GraphChangeSet) -> GraphChangeSet:
    auth_service = request.app.state.auth_service
    for id_field, username_field in (
        ("created_by", "created_by_username"),
        ("submitted_by", "submitted_by_username"),
        ("reviewed_by", "reviewed_by_username"),
        ("committed_by", "committed_by_username"),
    ):
        user_id = getattr(change_set, id_field, None)
        if not user_id or getattr(change_set, username_field, None):
            continue
        try:
            user = auth_service.get_user_by_id(UUID(str(user_id)))
        except Exception:
            user = None
        if user is not None:
            setattr(change_set, username_field, user.username)
    return change_set


def _graph_change_set_summary(change_set: GraphChangeSet) -> GraphChangeSetSummary:
    return GraphChangeSetSummary(
        change_set_id=change_set.change_set_id,
        project_id=change_set.project_id,
        source_note_id=change_set.source_note_id,
        source_note_ids=list(change_set.source_note_ids),
        source_checksum=change_set.source_checksum,
        source_content_type=change_set.source_content_type,
        source_filename=change_set.source_filename,
        source_note_count=change_set.source_note_count,
        batch_key=change_set.batch_key,
        batch_window_start=change_set.batch_window_start,
        batch_window_end=change_set.batch_window_end,
        provider=change_set.provider,
        model=change_set.model,
        prompt_version=change_set.prompt_version,
        draft_mode=change_set.draft_mode,
        summary=change_set.summary,
        uncertain_fields=list(change_set.uncertain_fields),
        clarification_requests=list(change_set.clarification_requests),
        status=change_set.status,
        commit_message=change_set.commit_message,
        error_metadata=dict(change_set.error_metadata),
        operation_count=change_set.operation_count,
        created_at=change_set.created_at,
        created_by=change_set.created_by,
        created_by_user_id=change_set.created_by_user_id,
        created_by_username=change_set.created_by_username,
        updated_at=change_set.updated_at,
        submitted_at=change_set.submitted_at,
        submitted_by=change_set.submitted_by,
        submitted_by_username=change_set.submitted_by_username,
        reviewed_at=change_set.reviewed_at,
        reviewed_by=change_set.reviewed_by,
        reviewed_by_username=change_set.reviewed_by_username,
        review_note=change_set.review_note,
        committed_at=change_set.committed_at,
        committed_by=change_set.committed_by,
        committed_by_username=change_set.committed_by_username,
    )


attach_graph_usernames = _attach_graph_usernames
