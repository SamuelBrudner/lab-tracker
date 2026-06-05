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
)
from lab_tracker.schemas import (
    Envelope,
    GoalCreateFields,
    GoalLinkCreate,
    GoalLinkUpdate,
    GoalUpdate,
    ListEnvelope,
)

from .shared import (
    actor_from_request,
    api_from_request,
    ensure_project_read,
    filter_project_scoped_items,
    list_response,
    paginate,
    repository_from_request,
    validate_pagination,
)


def build_goals_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

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
        ensure_project_read(request, project_id)
        goals, total = repository_from_request(request).query_goals(
            project_id=project_id,
            goal_type=goal_type.value if goal_type is not None else None,
            status=status.value if status is not None else None,
            limit=limit,
            offset=offset,
        )
        return list_response(goals, limit=limit, offset=offset, total=total)

    @router.get("/goals/{goal_id}", response_model=Envelope[Goal])
    def get_goal(goal_id: UUID, request: Request):
        goal = api_from_request(request, api).get_goal(goal_id)
        ensure_project_read(request, goal.project_id)
        return Envelope(data=goal)

    @router.patch("/goals/{goal_id}", response_model=Envelope[Goal])
    def update_goal(goal_id: UUID, payload: GoalUpdate, request: Request):
        actor = actor_from_request(request)
        goal = api_from_request(request, api).update_goal(
            goal_id,
            goal_type=payload.goal_type,
            title=payload.title,
            summary=payload.summary,
            status=payload.status,
            target_date=payload.target_date,
            external_ref=payload.external_ref,
            attributes=payload.attributes,
            actor=actor,
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
            relation=payload.relation,
            link_status=payload.link_status,
            slot=payload.slot,
            actor=actor,
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
        visible = filter_project_scoped_items(request, goals)
        items, total = paginate(visible, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    return router
