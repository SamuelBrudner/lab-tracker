"""Admin usage telemetry routes."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from io import StringIO
from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from starlette.requests import Request
from starlette.responses import Response

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import Role
from lab_tracker.errors import AuthError
from lab_tracker.schemas import Envelope, ListEnvelope

from .shared import actor_from_request, api_from_request, list_response, validate_pagination

_USAGE_EXPORT_FIELDS = [
    "event_id",
    "occurred_at",
    "verb",
    "resource_type",
    "resource_id",
    "actor_user_id",
    "actor_role",
    "principal_type",
    "surface",
    "project_id",
    "outcome",
    "duration_ms",
    "result_count",
]


def build_usage_events_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/usage-events/summary", response_model=Envelope[list[dict[str, object]]])
    def usage_event_summary(
        request: Request,
        start: datetime | None = None,
        end: datetime | None = None,
    ):
        _ensure_admin(request)
        summary = api_from_request(request, api).usage_event_summary(start=start, end=end)
        return Envelope(data=summary)

    @router.get("/usage-events", response_model=ListEnvelope[dict[str, object]])
    def list_usage_events(
        request: Request,
        project_id: UUID | None = None,
        verb: str | None = None,
        resource_type: str | None = None,
        surface: str | None = None,
        outcome: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        _ensure_admin(request)
        validate_pagination(limit, offset)
        events, total = api_from_request(request, api).query_usage_events(
            project_id=project_id,
            verb=verb,
            resource_type=resource_type,
            surface=surface,
            outcome=outcome,
            limit=limit,
            offset=offset,
        )
        items = [jsonable_encoder(event) for event in events]
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.get("/usage-events/export")
    def export_usage_events(
        request: Request,
        format: Literal["jsonl", "csv"] = "jsonl",
        project_id: UUID | None = None,
        verb: str | None = None,
        resource_type: str | None = None,
        surface: str | None = None,
        outcome: str | None = None,
    ):
        _ensure_admin(request)
        events, _ = api_from_request(request, api).query_usage_events(
            project_id=project_id,
            verb=verb,
            resource_type=resource_type,
            surface=surface,
            outcome=outcome,
            limit=None,
            offset=0,
        )
        encoded = [jsonable_encoder(event) for event in events]
        if format == "csv":
            body = _usage_events_csv(encoded)
            media_type = "text/csv"
            filename = "usage-events.csv"
        else:
            body = "\n".join(json.dumps(row, separators=(",", ":")) for row in encoded)
            if body:
                body += "\n"
            media_type = "application/x-ndjson"
            filename = "usage-events.jsonl"
        return Response(
            content=body,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.post("/usage-events/retention/run", response_model=Envelope[dict[str, int]])
    def run_usage_event_retention(request: Request):
        _ensure_admin(request)
        cutoff = datetime.now(timezone.utc) - timedelta(days=365)
        deleted = api_from_request(request, api).rollup_usage_events_before(cutoff)
        return Envelope(data={"raw_events_pruned": deleted})

    return router


def _usage_events_csv(rows: list[dict[str, object]]) -> str:
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_USAGE_EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def _ensure_admin(request: Request) -> None:
    actor = actor_from_request(request)
    if actor.role != Role.ADMIN:
        raise AuthError("Admin privileges required.")
