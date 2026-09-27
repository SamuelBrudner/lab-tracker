"""Typed request-state access for graph-draft provider clients."""

from __future__ import annotations

from starlette.requests import Request

from lab_tracker.config import Settings, get_settings
from lab_tracker.errors import ValidationError
from lab_tracker.graph_drafting import (
    GraphDraftClient,
    GraphDraftClientFactory,
    make_graph_draft_client,
)

EXTERNAL_PROVIDER_ACKNOWLEDGEMENT_REQUIRED = (
    "Drafting with an external AI provider requires explicit external-provider acknowledgement."
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


def require_external_provider_acknowledged(request: Request, *, acknowledged: bool) -> bool:
    """Refuse a note-scoped draft that would leave the host unacknowledged.

    A loopback provider keeps the note on this machine, so nothing is gated;
    any other provider host needs the person's explicit acknowledgement on
    the request. Returns the acknowledgement so the caller can record it.
    """

    external = settings_from_request(request).graph_draft_provider_is_external()
    if external and not acknowledged:
        raise ValidationError(EXTERNAL_PROVIDER_ACKNOWLEDGEMENT_REQUIRED)
    return acknowledged
