"""Goal routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import (
    EntityRef,
    EntityType,
    Goal,
    GoalLink,
    GoalLinkStatus,
    GoalStatus,
    GoalType,
    UsageEventResourceType,
)
from lab_tracker.patching import provided_fields
from lab_tracker.schemas import (
    Envelope,
    GoalCreate,
    GoalCreateFields,
    GoalLinkCreate,
    GoalLinkUpdate,
    GoalUpdate,
    ListEnvelope,
)
from lab_tracker.services.goal_service import GoalLinkSpec

from .shared import (
    actor_from_request,
    api_from_request,
    ensure_project_read,
    list_response,
    paginate,
    record_usage_view,
    validate_pagination,
)


def build_goals_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    def goal_link_specs(links: list[GoalLinkCreate] | None) -> list[GoalLinkSpec] | None:
        if links is None:
            return None
        return [
            GoalLinkSpec(
                target=EntityRef(entity_type=link.entity_type, entity_id=link.entity_id),
                relation=link.relation,
                link_status=link.link_status,
                slot=link.slot,
            )
            for link in links
        ]

    @router.post(
        "/goals",
        response_model=Envelope[Goal],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_spanning_goal(payload: GoalCreate, request: Request):
        actor = actor_from_request(request)
        goal = api_from_request(request, api).create_goal(
            project_id=payload.project_id,
            goal_type=payload.goal_type,
            title=payload.title,
            summary=payload.summary,
            status=payload.status or GoalStatus.PLANNED,
            target_date=payload.target_date,
            external_ref=payload.external_ref,
            attributes=payload.attributes,
            links=goal_link_specs(payload.links),
            actor=actor,
        )
        return Envelope(data=goal)

    @router.get("/goals", response_model=ListEnvelope[Goal])
    def list_visible_goals(
        request: Request,
        project_id: UUID | None = None,
        goal_type: GoalType | None = None,
        status: GoalStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        actor = actor_from_request(request)
        goals = api_from_request(request, api).goals.list_visible_goals(
            project_id=project_id,
            goal_type=goal_type,
            status=status,
            actor=actor,
        )
        items, total = paginate(goals, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.post(
        "/projects/{project_id}/goals",
        response_model=Envelope[Goal],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_goal(project_id: UUID, payload: GoalCreateFields, request: Request):
        actor = actor_from_request(request)
        goal = api_from_request(request, api).create_goal(
            project_id=project_id,
            goal_type=payload.goal_type,
            title=payload.title,
            summary=payload.summary,
            status=payload.status or GoalStatus.PLANNED,
            target_date=payload.target_date,
            external_ref=payload.external_ref,
            attributes=payload.attributes,
            actor=actor,
        )
        return Envelope(data=goal)

    @router.get("/projects/{project_id}/goals", response_model=ListEnvelope[Goal])
    def list_goals(
        project_id: UUID,
        request: Request,
        goal_type: GoalType | None = None,
        status: GoalStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        actor = actor_from_request(request)
        goals = api_from_request(request, api).goals.list_visible_goals(
            project_id=project_id,
            goal_type=goal_type,
            status=status,
            actor=actor,
        )
        items, total = paginate(goals, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.get("/goals/{goal_id}", response_model=Envelope[Goal])
    def get_goal(goal_id: UUID, request: Request):
        request_api = api_from_request(request, api)
        actor = actor_from_request(request)
        goal = request_api.get_goal(goal_id)
        request_api.require_goal_read(goal, actor=actor)
        record_usage_view(
            request,
            resource_type=UsageEventResourceType.GOAL,
            resource_id=goal.goal_id,
            project_id=goal.project_id,
        )
        return Envelope(data=goal)

    @router.patch("/goals/{goal_id}", response_model=Envelope[Goal])
    def update_goal(goal_id: UUID, payload: GoalUpdate, request: Request):
        actor = actor_from_request(request)
        updates = provided_fields(payload)
        if "links" in updates:
            updates["links"] = goal_link_specs(updates["links"])
        goal = api_from_request(request, api).update_goal(
            goal_id,
            actor=actor,
            **updates,
        )
        return Envelope(data=goal)

    @router.delete("/goals/{goal_id}", response_model=Envelope[Goal])
    def delete_goal(goal_id: UUID, request: Request):
        actor = actor_from_request(request)
        goal = api_from_request(request, api).delete_goal(goal_id, actor=actor)
        return Envelope(data=goal)

    @router.post(
        "/goals/{goal_id}/links",
        response_model=Envelope[GoalLink],
        status_code=http_status.HTTP_201_CREATED,
    )
    def link_node_to_goal(goal_id: UUID, payload: GoalLinkCreate, request: Request):
        actor = actor_from_request(request)
        link = api_from_request(request, api).link_node_to_goal(
            goal_id,
            target=EntityRef(entity_type=payload.entity_type, entity_id=payload.entity_id),
            relation=payload.relation,
            link_status=payload.link_status or GoalLinkStatus.CANDIDATE,
            slot=payload.slot,
            actor=actor,
        )
        return Envelope(data=link)

    @router.patch("/goals/{goal_id}/links/{link_id}", response_model=Envelope[GoalLink])
    def update_goal_link(
        goal_id: UUID,
        link_id: UUID,
        payload: GoalLinkUpdate,
        request: Request,
    ):
        actor = actor_from_request(request)
        link = api_from_request(request, api).update_goal_link(
            goal_id,
            link_id,
            actor=actor,
            **provided_fields(payload),
        )
        return Envelope(data=link)

    @router.delete("/goals/{goal_id}/links/{link_id}", response_model=Envelope[GoalLink])
    def delete_goal_link(goal_id: UUID, link_id: UUID, request: Request):
        actor = actor_from_request(request)
        link = api_from_request(request, api).delete_goal_link(
            goal_id,
            link_id,
            actor=actor,
        )
        return Envelope(data=link)

    @router.get(
        "/projects/{project_id}/nodes/{entity_type}/{entity_id}/goals",
        response_model=ListEnvelope[Goal],
    )
    def list_node_goals(
        project_id: UUID,
        entity_type: EntityType,
        entity_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        ensure_project_read(request, project_id)
        goals = api_from_request(request, api).list_node_goals(
            project_id=project_id,
            target=EntityRef(entity_type=entity_type, entity_id=entity_id),
        )
        request_api = api_from_request(request, api)
        actor = actor_from_request(request)
        visible = [
            goal for goal in goals if request_api.goals.can_read_goal(goal, actor=actor)
        ]
        items, total = paginate(visible, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    return router
