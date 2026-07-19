"""Schema and vocabulary discovery routes for agent and client ergonomics."""

from __future__ import annotations

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from lab_tracker.schema_metadata import JsonObject, build_schema_description
from lab_tracker.vocabulary import build_terms_document, build_terms_html

from .shared import provenance_base_url


def build_schema_router() -> APIRouter:
    router = APIRouter()

    @router.get("/schema/describe")
    def describe_schema(entity_type: str | None = None) -> JsonObject:
        """Describe Lab Tracker fields, enum values, and lifecycle transitions."""
        return {"data": build_schema_description(entity_type=entity_type)}

    @router.get("/terms")
    def vocabulary_terms(request: Request) -> Response:
        """The vocabulary that ``lab:`` IRIs in provenance documents resolve to.

        Content-negotiated: JSON-LD for machines, HTML for people.
        """
        base_url = provenance_base_url(request)
        accept = request.headers.get("accept", "")
        wants_jsonld = "application/ld+json" in accept or "application/json" in accept
        if wants_jsonld and "text/html" not in accept:
            return JSONResponse(
                content=build_terms_document(base_url),
                media_type="application/ld+json",
            )
        return HTMLResponse(content=build_terms_html(base_url))

    return router
