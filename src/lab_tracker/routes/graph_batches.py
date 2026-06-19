"""Scheduled graph draft batch routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.config import get_settings
from lab_tracker.graph_drafting import make_graph_draft_client
from lab_tracker.models import (
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftBatchRun,
    GraphDraftBatchRunStatus,
    GraphDraftBatchSettings,
    UsageEventResourceType,
)
from lab_tracker.schemas import (
    Envelope,
    GraphDraftBatchRunRequest,
    GraphDraftBatchSettingsUpdate,
    ListEnvelope,
)

from .graph_drafts import attach_graph_usernames
from .shared import (
    actor_from_request,
    api_from_request,
    ensure_project_owner,
    ensure_project_read,
    filter_project_scoped_items,
    list_response,
    paginate,
    record_usage_view,
    validate_pagination,
)

_PENDING_BATCH_STATUSES = {
    GraphChangeSetStatus.READY,
    GraphChangeSetStatus.SUBMITTED,
    GraphChangeSetStatus.CHANGES_REQUESTED,
}


def build_graph_batches_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/batches", response_model=ListEnvelope[GraphChangeSet])
    def list_batches(
        request: Request,
        project_id: UUID | None = None,
        status: GraphChangeSetStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        if project_id is not None:
            ensure_project_read(request, project_id)
        change_sets = api_from_request(request, api).list_batch_graph_drafts(
            project_id=project_id,
            status=status,
        )
        if status is None:
            change_sets = [
                change_set
                for change_set in change_sets
                if change_set.status in _PENDING_BATCH_STATUSES
            ]
        visible = filter_project_scoped_items(request, change_sets)
        items, total = paginate(visible, limit, offset)
        return list_response(
            [attach_graph_usernames(request, item) for item in items],
            limit=limit,
            offset=offset,
            total=total,
        )

    @router.get("/batches/{change_set_id:uuid}", response_model=Envelope[GraphChangeSet])
    def get_batch(change_set_id: UUID, request: Request):
        change_set = api_from_request(request, api).get_graph_change_set(change_set_id)
        ensure_project_read(request, change_set.project_id)
        record_usage_view(
            request,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            resource_id=change_set.change_set_id,
            project_id=change_set.project_id,
        )
        return Envelope(data=attach_graph_usernames(request, change_set))

    @router.get(
        "/projects/{project_id:uuid}/graph-draft-batch-settings",
        response_model=Envelope[GraphDraftBatchSettings],
    )
    def get_batch_settings(project_id: UUID, request: Request):
        actor = actor_from_request(request)
        settings = api_from_request(request, api).get_graph_draft_batch_settings(
            project_id,
            actor=actor,
        )
        record_usage_view(
            request,
            resource_type=UsageEventResourceType.GRAPH_DRAFT_BATCH_SETTINGS,
            resource_id=settings.settings_id,
            project_id=settings.project_id,
        )
        return Envelope(data=settings)

    @router.patch(
        "/projects/{project_id:uuid}/graph-draft-batch-settings",
        response_model=Envelope[GraphDraftBatchSettings],
    )
    def update_batch_settings(
        project_id: UUID,
        payload: GraphDraftBatchSettingsUpdate,
        request: Request,
    ):
        actor = actor_from_request(request)
        settings = api_from_request(request, api).update_graph_draft_batch_settings(
            project_id,
            enabled=payload.enabled,
            cadence_minutes=payload.cadence_minutes,
            run_at_local_time=payload.run_at_local_time,
            timezone_name=payload.timezone_name,
            actor=actor,
        )
        return Envelope(data=settings)

    @router.get("/batches/runs", response_model=ListEnvelope[GraphDraftBatchRun])
    def list_batch_runs(
        request: Request,
        project_id: UUID | None = None,
        status: GraphDraftBatchRunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        if project_id is not None:
            ensure_project_read(request, project_id)
        runs = api_from_request(request, api).list_graph_draft_batch_runs(
            project_id=project_id,
            status=status,
        )
        visible = filter_project_scoped_items(request, runs)
        items, total = paginate(visible, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.post(
        "/batches/run-now",
        response_model=Envelope[GraphDraftBatchRun],
        status_code=http_status.HTTP_201_CREATED,
    )
    def run_batch_now(payload: GraphDraftBatchRunRequest, request: Request):
        actor = actor_from_request(request)
        ensure_project_owner(request, payload.project_id)
        draft_client = _draft_client_from_request(request)
        try:
            run = api_from_request(request, api).run_graph_draft_batch_for_project(
                payload.project_id,
                draft_client=draft_client,
                since=payload.since,
                until=payload.until,
                user_hint=payload.user_hint,
                actor=actor,
            )
        finally:
            close = getattr(draft_client, "close", None)
            if callable(close):
                close()
        return Envelope(data=run)

    @router.post("/batches/run-due", response_model=ListEnvelope[GraphDraftBatchRun])
    def run_due_batches(request: Request):
        actor = actor_from_request(request)
        runs = api_from_request(request, api).run_due_graph_draft_batches(
            draft_client_factory=_draft_client_factory_from_request(request),
            app_settings=getattr(request.app.state, "settings", None) or get_settings(),
            actor=actor,
        )
        return list_response(runs, limit=max(1, len(runs) or 1), offset=0, total=len(runs))

    return router


def _draft_client_factory_from_request(request: Request):
    factory = getattr(request.app.state, "graph_draft_client_factory", None)
    if callable(factory):
        return factory
    return make_graph_draft_client


def _draft_client_from_request(request: Request):
    settings = getattr(request.app.state, "settings", None) or get_settings()
    return _draft_client_factory_from_request(request)(settings)
