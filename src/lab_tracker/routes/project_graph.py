"""Project graph visualization routes."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.project_graph import project_graph_to_mermaid
from lab_tracker.schemas import (
    Envelope,
    GraphEntityType,
    GraphNeighborhoodRead,
    GraphOverviewRead,
    GraphRetrievalMode,
    GraphSearchRead,
    GraphTraversalDirection,
    PersistedGraphEntityType,
    ProjectGraphRead,
    ProjectGraphView,
)

from .shared import actor_from_request, handlers_from_request


def build_project_graph_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/projects/{project_id}/graph", response_model=Envelope[ProjectGraphRead])
    def get_project_graph(
        project_id: UUID,
        request: Request,
        view: ProjectGraphView = "evidence",
    ):
        graph = handlers_from_request(request).context.project_graph(
            project_id,
            actor=actor_from_request(request),
            view=view,
        )
        return Envelope(data=graph)

    @router.get(
        "/projects/{project_id}/graph/overview",
        response_model=Envelope[GraphOverviewRead],
    )
    def get_graph_overview(project_id: UUID, request: Request):
        overview = handlers_from_request(request).context.graph_overview(
            project_id,
            actor=actor_from_request(request),
        )
        return Envelope(data=overview)

    @router.get(
        "/projects/{project_id}/graph/search",
        response_model=Envelope[GraphSearchRead],
    )
    def search_project_graph(
        project_id: UUID,
        request: Request,
        q: Annotated[str, Query(min_length=2, max_length=256)],
        entity_types: Annotated[
            list[PersistedGraphEntityType] | None,
            Query(),
        ] = None,
        statuses: Annotated[list[str] | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
        retrieval_mode: GraphRetrievalMode = "auto",
    ):
        results = handlers_from_request(request).context.search_graph(
            project_id,
            actor=actor_from_request(request),
            query=q,
            entity_types=list(entity_types) if entity_types is not None else None,
            statuses=statuses,
            limit=limit,
            offset=offset,
            retrieval_mode=retrieval_mode,
        )
        return Envelope(
            data=results,
            meta={"count": len(results.items), "has_more": results.has_more},
        )

    @router.get(
        "/projects/{project_id}/graph/neighborhood/{entity_type}/{entity_id}",
        response_model=Envelope[GraphNeighborhoodRead],
    )
    def get_graph_neighborhood(
        project_id: UUID,
        entity_type: PersistedGraphEntityType,
        entity_id: UUID,
        request: Request,
        direction: GraphTraversalDirection = "both",
        relationships: Annotated[list[str] | None, Query()] = None,
        node_types: Annotated[list[GraphEntityType] | None, Query()] = None,
        depth: Annotated[int, Query(ge=1, le=2)] = 1,
        max_nodes: Annotated[int, Query(ge=1, le=200)] = 50,
        max_edges: Annotated[int, Query(ge=1, le=500)] = 100,
        include_anchor_content: bool = False,
    ):
        neighborhood = handlers_from_request(request).context.graph_neighborhood(
            project_id,
            actor=actor_from_request(request),
            anchor_type=entity_type,
            anchor_id=entity_id,
            direction=direction,
            relationships=relationships,
            node_types=list(node_types) if node_types is not None else None,
            depth=depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
            include_anchor_content=include_anchor_content,
        )
        return Envelope(
            data=neighborhood,
            meta={
                "nodes_count": len(neighborhood.nodes),
                "edges_count": len(neighborhood.edges),
                "truncated": neighborhood.truncation.truncated,
            },
        )

    @router.get("/projects/{project_id}/graph/mermaid")
    def get_project_graph_mermaid(
        project_id: UUID,
        request: Request,
        view: ProjectGraphView = "evidence",
    ):
        graph = handlers_from_request(request).context.project_graph(
            project_id,
            actor=actor_from_request(request),
            view=view,
        )
        return PlainTextResponse(
            project_graph_to_mermaid(graph),
            media_type="text/vnd.mermaid",
        )

    return router
