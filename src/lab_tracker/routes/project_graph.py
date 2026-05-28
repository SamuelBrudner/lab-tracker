"""Project graph visualization routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.project_graph import build_project_graph, project_graph_to_mermaid
from lab_tracker.schemas import Envelope, ProjectGraphRead, ProjectGraphView

from .shared import api_from_request, ensure_project_read, repository_from_request


def build_project_graph_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/projects/{project_id}/graph", response_model=Envelope[ProjectGraphRead])
    def get_project_graph(
        project_id: UUID,
        request: Request,
        view: ProjectGraphView = "evidence",
    ):
        project = api_from_request(request, api).get_project(project_id)
        ensure_project_read(request, project.project_id)
        graph = build_project_graph(repository_from_request(request), project.project_id, view=view)
        return Envelope(data=graph)

    @router.get("/projects/{project_id}/graph/mermaid")
    def get_project_graph_mermaid(
        project_id: UUID,
        request: Request,
        view: ProjectGraphView = "evidence",
    ):
        project = api_from_request(request, api).get_project(project_id)
        ensure_project_read(request, project.project_id)
        graph = build_project_graph(repository_from_request(request), project.project_id, view=view)
        return PlainTextResponse(
            project_graph_to_mermaid(graph),
            media_type="text/vnd.mermaid",
        )

    return router
