"""Schema discovery routes for agent and client ergonomics."""

from __future__ import annotations

from fastapi import APIRouter

from lab_tracker.schema_metadata import JsonObject, build_schema_description


def build_schema_router() -> APIRouter:
    router = APIRouter()

    @router.get("/schema/describe")
    def describe_schema(entity_type: str | None = None) -> JsonObject:
        """Describe Lab Tracker fields, enum values, and lifecycle transitions."""
        return {"data": build_schema_description(entity_type=entity_type)}

    return router
