"""Scheduled graph draft batch routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext
from lab_tracker.config import get_settings
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftBatchRun,
    GraphDraftBatchRunStatus,
    GraphDraftBatchSettings,
    ProjectMembershipRole,
    UsageEventResourceType,
)
from lab_tracker.patching import provided_fields
from lab_tracker.schemas import (
    Envelope,
    GraphBatchSummary,
    GraphDraftBatchRunRequest,
    GraphDraftBatchSettingsUpdate,
    ListEnvelope,
)
from lab_tracker.services.graph_draft_batch_policy import BatchReviewQuery, BatchRunQuery

from .graph_draft_clients import (
    draft_client_factory_from_request as _draft_client_factory_from_request,
)
from .graph_draft_clients import draft_client_from_request as _draft_client_from_request
from .graph_drafts import attach_graph_usernames, graph_change_set_summary
from .shared import (
    accessible_project_ids_from_request,
    actor_from_request,
    api_from_request,
    ensure_project_contributor,
    ensure_project_owner,
    ensure_project_read,
    list_response,
    record_usage_view,
    validate_pagination,
)

_PENDING_BATCH_STATUSES = frozenset(
    {
        GraphChangeSetStatus.READY,
        GraphChangeSetStatus.SUBMITTED,
        GraphChangeSetStatus.CHANGES_REQUESTED,
    }
)
_PERSONAL_ACTION_STATUSES = frozenset(
    {
        GraphChangeSetStatus.READY,
        GraphChangeSetStatus.CHANGES_REQUESTED,
    }
)


def build_graph_batches_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/batches", response_model=ListEnvelope[GraphBatchSummary])
    def list_batches(
        request: Request,
        project_id: UUID | None = None,
        status: GraphChangeSetStatus | None = None,
        mine: bool = False,
        needs_commit: bool = False,
        unassigned_oversight: bool = False,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        if sum((mine, needs_commit, unassigned_oversight)) > 1:
            raise ValidationError(
                "mine, needs_commit, and unassigned_oversight are separate "
                "Daily Review views."
            )
        if needs_commit and status not in {None, GraphChangeSetStatus.SUBMITTED}:
            raise ValidationError("needs_commit only applies to submitted Daily Reviews.")
        if unassigned_oversight and status not in {
            None,
            GraphChangeSetStatus.READY,
            GraphChangeSetStatus.CHANGES_REQUESTED,
        }:
            raise ValidationError(
                "unassigned_oversight only applies to actionable Daily Reviews."
            )
        if project_id is not None and unassigned_oversight:
            ensure_project_owner(request, project_id)
        elif project_id is not None:
            ensure_project_read(request, project_id)
        effective_status = GraphChangeSetStatus.SUBMITTED if needs_commit else status
        if effective_status is not None:
            statuses = frozenset({effective_status})
        elif mine or unassigned_oversight:
            statuses = _PERSONAL_ACTION_STATUSES
        else:
            statuses = _PENDING_BATCH_STATUSES
        request_api = api_from_request(request, api)
        project_scope = (
            _owner_project_scope(request, request_api, project_id)
            if needs_commit or unassigned_oversight
            else _read_project_scope(request, project_id)
        )
        change_sets, total = request_api.query_batch_graph_drafts(
            BatchReviewQuery(
                statuses=statuses,
                project_ids=project_scope,
                assigned_to_user_id=_personal_queue_user_id(request, mine=mine),
                unassigned_only=unassigned_oversight,
                limit=limit,
                offset=offset,
            )
        )
        return list_response(
            [
                _batch_summary(attach_graph_usernames(request, change_set))
                for change_set in change_sets
            ],
            limit=limit,
            offset=offset,
            total=total,
        )

    @router.get("/batches/{change_set_id:uuid}", response_model=Envelope[GraphChangeSet])
    def get_batch(change_set_id: UUID, request: Request):
        actor = actor_from_request(request)
        change_set = api_from_request(request, api).get_graph_change_set_for_read(
            change_set_id,
            actor=actor,
        )
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
    def get_batch_settings(
        project_id: UUID,
        request: Request,
        user_id: UUID | None = None,
    ):
        actor = actor_from_request(request)
        personal_user_id = _personal_settings_user_id(request, actor)
        if personal_user_id is None and user_id is not None:
            raise ValidationError(
                "Auth-disabled Daily Review uses one legacy settings bucket."
            )
        settings_user_id = user_id if user_id is not None else personal_user_id
        settings = api_from_request(request, api).get_graph_draft_batch_settings(
            project_id,
            user_id=settings_user_id,
            actor=actor,
        )
        record_usage_view(
            request,
            resource_type=UsageEventResourceType.GRAPH_DRAFT_BATCH_SETTINGS,
            resource_id=settings.settings_id,
            project_id=settings.project_id,
        )
        return Envelope(data=settings)

    @router.get(
        "/projects/{project_id:uuid}/graph-draft-batch-settings/project-default",
        response_model=Envelope[GraphDraftBatchSettings],
    )
    def get_project_default_batch_settings(project_id: UUID, request: Request):
        ensure_project_owner(request, project_id)
        actor = actor_from_request(request)
        settings = api_from_request(request, api).get_graph_draft_batch_settings(
            project_id,
            user_id=None,
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
        fields = provided_fields(payload)
        personal_user_id = _personal_settings_user_id(request, actor)
        if "user_id" in fields:
            raise ValidationError(
                "Personal Daily Review settings resolve the authenticated user; "
                "user_id is not accepted."
            )
        if personal_user_id is not None:
            fields["user_id"] = personal_user_id
        settings = api_from_request(request, api).update_graph_draft_batch_settings(
            project_id,
            actor=actor,
            **fields,
        )
        return Envelope(data=settings)

    @router.patch(
        "/projects/{project_id:uuid}/graph-draft-batch-settings/project-default",
        response_model=Envelope[GraphDraftBatchSettings],
    )
    def update_project_default_batch_settings(
        project_id: UUID,
        payload: GraphDraftBatchSettingsUpdate,
        request: Request,
    ):
        ensure_project_owner(request, project_id)
        fields = provided_fields(payload)
        if "user_id" in fields:
            raise ValidationError("Project-default settings cannot target a user_id.")
        settings = api_from_request(request, api).update_graph_draft_batch_settings(
            project_id,
            actor=actor_from_request(request),
            **fields,
        )
        return Envelope(data=settings)

    @router.get("/batches/runs", response_model=ListEnvelope[GraphDraftBatchRun])
    def list_batch_runs(
        request: Request,
        project_id: UUID | None = None,
        status: GraphDraftBatchRunStatus | None = None,
        mine: bool = False,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        if project_id is not None:
            ensure_project_read(request, project_id)
        runs, total = api_from_request(request, api).query_graph_draft_batch_runs(
            BatchRunQuery(
                project_ids=_read_project_scope(request, project_id),
                status=status,
                assigned_to_user_id=_personal_queue_user_id(request, mine=mine),
                limit=limit,
                offset=offset,
            )
        )
        return list_response(runs, limit=limit, offset=offset, total=total)

    @router.post(
        "/batches/run-now",
        response_model=Envelope[GraphDraftBatchRun],
        status_code=http_status.HTTP_201_CREATED,
    )
    def run_batch_now(payload: GraphDraftBatchRunRequest, request: Request):
        actor = actor_from_request(request)
        ensure_project_contributor(request, payload.project_id)
        if _background_drafting_enabled(request):
            run = api_from_request(request, api).enqueue_graph_draft_batch_for_project(
                payload.project_id,
                since=payload.since,
                until=payload.until,
                user_hint=payload.user_hint,
                actor=actor,
            )
            return Envelope(data=run)
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
        if _background_drafting_enabled(request):
            runs = api_from_request(request, api).enqueue_due_graph_draft_batches(
                actor=actor,
            )
            return list_response(
                runs,
                limit=max(1, len(runs) or 1),
                offset=0,
                total=len(runs),
            )
        runs = api_from_request(request, api).run_due_graph_draft_batches(
            draft_client_factory=_draft_client_factory_from_request(request),
            app_settings=getattr(request.app.state, "settings", None) or get_settings(),
            actor=actor,
        )
        return list_response(runs, limit=max(1, len(runs) or 1), offset=0, total=len(runs))

    return router


def _read_project_scope(
    request: Request,
    project_id: UUID | None,
) -> frozenset[UUID] | None:
    """Projects a list view may show: the checked project, or every readable one.

    ``None`` means unrestricted (global readers such as admins).
    """

    if project_id is not None:
        return frozenset({project_id})
    accessible = accessible_project_ids_from_request(request)
    return None if accessible is None else frozenset(accessible)


def _owner_project_scope(
    request: Request,
    request_api: LabTrackerAPI,
    project_id: UUID | None,
) -> frozenset[UUID] | None:
    """Projects the actor owns, bounded by project count rather than batch history."""

    actor = actor_from_request(request)
    candidates = _read_project_scope(request, project_id)
    if candidates is None:
        # Only global readers see every project, and they own them all.
        return None
    return frozenset(
        candidate
        for candidate in candidates
        if request_api.project_membership_role(candidate, actor)
        == ProjectMembershipRole.OWNER
    )


def _personal_queue_user_id(request: Request, *, mine: bool) -> UUID | None:
    # Auth-disabled deployments have one reviewer bucket, so "mine" is the
    # whole (legacy) queue rather than an assignee filter.
    if not mine or not getattr(request.app.state, "auth_enabled", True):
        return None
    return actor_from_request(request).user_id


def _batch_summary(change_set: GraphChangeSet) -> GraphBatchSummary:
    return GraphBatchSummary(
        **graph_change_set_summary(change_set).model_dump(),
        meeting_note_count=change_set.meeting_note_count,
    )


def _personal_settings_user_id(
    request: Request,
    actor: AuthContext,
) -> UUID | None:
    # Auth-disabled deployments deliberately keep one legacy settings bucket:
    # their notes have no persisted attribution FK, so a synthetic per-user
    # row would never select them for scheduled review.
    if not getattr(request.app.state, "auth_enabled", True):
        return None
    return actor.user_id


def _background_drafting_enabled(request: Request) -> bool:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    return bool(
        getattr(settings, "graph_draft_background_enabled", False)
        or getattr(settings, "graph_draft_scheduler_enabled", False)
    )
