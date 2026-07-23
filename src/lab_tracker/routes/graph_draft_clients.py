"""Typed request-state access for graph-draft provider clients."""

from __future__ import annotations

from starlette.requests import Request

from lab_tracker.config import Settings, get_settings
from lab_tracker.graph_drafting import (
    GraphDraftClient,
    GraphDraftClientFactory,
    make_graph_draft_client,
)


def settings_from_request(request: Request) -> Settings:
    settings: Settings | None = getattr(request.app.state, "settings", None)
    return settings or get_settings()


def draft_client_factory_from_request(request: Request) -> GraphDraftClientFactory:
    factory: GraphDraftClientFactory | None = getattr(
        request.app.state,
        "graph_draft_client_factory",
        None,
    )
    if callable(factory):
        return factory
    return make_graph_draft_client


def draft_client_from_request(request: Request) -> GraphDraftClient:
    return draft_client_factory_from_request(request)(settings_from_request(request))
