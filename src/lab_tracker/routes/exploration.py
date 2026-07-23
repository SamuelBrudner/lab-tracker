"""Exploration trajectory routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import (
    EntityType,
    ExplorationNode,
    ExplorationNodeStatus,
    ExplorationNodeType,
    UsageEventResourceType,
)
from lab_tracker.patching import provided_fields
from lab_tracker.schemas import (
    Envelope,
    ExplorationNodeCreate,
    ExplorationNodeUpdate,
    ListEnvelope,
)

from .shared import (
    CreatedByFilter,
    accessible_project_ids_from_request,
    actor_from_request,
    api_from_request,
    ensure_project_read,
    list_response,
    record_usage_view,
    repository_from_request,
    validate_pagination,
)


def build_exploration_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/exploration-nodes",
        response_model=Envelope[ExplorationNode],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_exploration_node(payload: ExplorationNodeCreate, request: Request):
        actor = actor_from_request(request)
        node = api_from_request(request, api).create_exploration_node(
            project_id=payload.project_id,
            node_type=payload.node_type,
            title=payload.title,
            target=payload.target,
            status=payload.status or ExplorationNodeStatus.STAGED,
            choice=payload.choice,
            alternatives_considered=payload.alternatives_considered,
            rationale=payload.rationale,
            evidence_refs=payload.evidence_refs,
            hypothesis=payload.hypothesis,
            failure_mode=payload.failure_mode,
            lesson=payload.lesson,
            tooling_context=payload.tooling_context,
            trigger=payload.trigger,
            invalidates_node_id=payload.invalidates_node_id,
            invalidates_claim_id=payload.invalidates_claim_id,
            parent_node_ids=payload.parent_node_ids,
            also_depends_on_node_ids=payload.also_depends_on_node_ids,
            actor=actor,
        )
        return Envelope(data=node)

    @router.get("/exploration-nodes", response_model=ListEnvelope[ExplorationNode])
    def list_exploration_nodes(
        request: Request,
        project_id: UUID | None = None,
        node_type: ExplorationNodeType | None = None,
        status: ExplorationNodeStatus | None = None,
        target_entity_type: EntityType | None = None,
        target_entity_id: UUID | None = None,
        created_by: CreatedByFilter = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        if project_id is not None:
            ensure_project_read(request, project_id)
            project_ids = None
        else:
            project_ids = accessible_project_ids_from_request(request)
        nodes, total = repository_from_request(request).query_exploration_nodes(
            project_id=project_id,
            project_ids=project_ids,
            node_type=node_type.value if node_type is not None else None,
            status=status.value if status is not None else None,
            target_entity_type=(
                target_entity_type.value if target_entity_type is not None else None
            ),
            target_entity_id=target_entity_id,
            created_by=created_by,
            limit=limit,
            offset=offset,
        )
        return list_response(nodes, limit=limit, offset=offset, total=total)

    @router.get("/exploration-nodes/{node_id}", response_model=Envelope[ExplorationNode])
    def get_exploration_node(node_id: UUID, request: Request):
        node = api_from_request(request, api).get_exploration_node(node_id)
        ensure_project_read(request, node.project_id)
        record_usage_view(
            request,
            resource_type=UsageEventResourceType.EXPLORATION_NODE,
            resource_id=node.node_id,
            project_id=node.project_id,
        )
        return Envelope(data=node)

    @router.patch("/exploration-nodes/{node_id}", response_model=Envelope[ExplorationNode])
    def update_exploration_node(
        node_id: UUID,
        payload: ExplorationNodeUpdate,
        request: Request,
    ):
        actor = actor_from_request(request)
        existing = api_from_request(request, api).get_exploration_node(node_id)
        ensure_project_read(request, existing.project_id)
        node = api_from_request(request, api).update_exploration_node(
            node_id,
            actor=actor,
            **provided_fields(payload),
        )
        return Envelope(data=node)

    @router.delete("/exploration-nodes/{node_id}", response_model=Envelope[ExplorationNode])
    def delete_exploration_node(node_id: UUID, request: Request):
        actor = actor_from_request(request)
        existing = api_from_request(request, api).get_exploration_node(node_id)
        ensure_project_read(request, existing.project_id)
        node = api_from_request(request, api).delete_exploration_node(node_id, actor=actor)
        return Envelope(data=node)

    return router
