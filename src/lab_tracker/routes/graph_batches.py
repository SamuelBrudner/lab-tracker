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
    GraphDraftBatchRunRequest,
    GraphDraftBatchSettingsUpdate,
    ListEnvelope,
)

from .graph_draft_clients import (
    draft_client_factory_from_request as _draft_client_factory_from_request,
)
from .graph_draft_clients import draft_client_from_request as _draft_client_from_request
from .graph_drafts import attach_graph_usernames
from .shared import (
    actor_from_request,
    api_from_request,
    ensure_project_contributor,
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
_PERSONAL_ACTION_STATUSES = {
    GraphChangeSetStatus.READY,
    GraphChangeSetStatus.CHANGES_REQUESTED,
}


def build_graph_batches_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/batches", response_model=ListEnvelope[GraphChangeSet])
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
        change_sets = api_from_request(request, api).list_batch_graph_drafts(
            project_id=project_id,
            status=effective_status,
        )
        if effective_status is None:
            default_statuses = (
                _PERSONAL_ACTION_STATUSES
                if mine or unassigned_oversight
                else _PENDING_BATCH_STATUSES
            )
            change_sets = [
                change_set for change_set in change_sets if change_set.status in default_statuses
            ]
        visible = filter_project_scoped_items(request, change_sets)
        if mine and getattr(request.app.state, "auth_enabled", True):
            actor = actor_from_request(request)
            visible = [
                change_set for change_set in visible if _assigned_to_actor(change_set, actor)
            ]
        if needs_commit:
            actor = actor_from_request(request)
            request_api = api_from_request(request, api)
            visible = [
                change_set
                for change_set in visible
                if request_api.project_membership_role(change_set.project_id, actor)
                == ProjectMembershipRole.OWNER
            ]
        if unassigned_oversight:
            actor = actor_from_request(request)
            request_api = api_from_request(request, api)
            owner_visible = [
                change_set
                for change_set in visible
                if request_api.project_membership_role(change_set.project_id, actor)
                == ProjectMembershipRole.OWNER
            ]
            visible = [
                change_set
                for change_set in owner_visible
                if change_set.review_assignee_user_id is None
                and change_set.review_assignee is None
            ]
        items, total = paginate(visible, limit, offset)
        return list_response(
            [attach_graph_usernames(request, item) for item in items],
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
        runs = api_from_request(request, api).list_graph_draft_batch_runs(
            project_id=project_id,
            status=status,
        )
        visible = filter_project_scoped_items(request, runs)
        if mine and getattr(request.app.state, "auth_enabled", True):
            actor = actor_from_request(request)
            visible = [run for run in visible if _assigned_to_actor(run, actor)]
        items, total = paginate(visible, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

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


def _assigned_to_actor(
    item: GraphChangeSet | GraphDraftBatchRun,
    actor: AuthContext,
) -> bool:
    if item.review_assignee_user_id is not None:
        return item.review_assignee_user_id == actor.user_id
    actor_id = str(actor.user_id)
    if item.review_assignee is not None:
        return item.review_assignee == actor_id
    # Rows with no assignee are legacy project-oversight work. Creator
    # attribution (often SYSTEM/admin for scheduled drafts) is not assignment.
    return False


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
