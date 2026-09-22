"""Admin usage telemetry routes."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import StringIO
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from starlette.requests import Request
from starlette.responses import StreamingResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import Role
from lab_tracker.errors import AuthError
from lab_tracker.models import UsageEvent
from lab_tracker.schemas import Envelope, ListEnvelope

from .shared import actor_from_request, api_from_request, list_response, validate_pagination

# Rows read per database round trip while streaming /usage-events/export.
_USAGE_EXPORT_PAGE_SIZE = 1000

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
        filters = _UsageExportFilters(
            project_id=project_id,
            verb=verb,
            resource_type=resource_type,
            surface=surface,
            outcome=outcome,
        )
        # The first page is read in the request scope so query failures still
        # become ordinary error responses before any bytes are streamed.
        first_page = api_from_request(request, api).page_usage_events(
            **filters.as_kwargs(),
            after=None,
            limit=_USAGE_EXPORT_PAGE_SIZE,
        )
        pages = _usage_event_export_pages(request.app, filters, first_page)
        if format == "csv":
            body = _usage_events_csv_chunks(pages)
            media_type = "text/csv"
            filename = "usage-events.csv"
        else:
            body = _usage_events_jsonl_chunks(pages)
            media_type = "application/x-ndjson"
            filename = "usage-events.jsonl"
        return StreamingResponse(
            body,
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


@dataclass(frozen=True)
class _UsageExportFilters:
    project_id: UUID | None
    verb: str | None
    resource_type: str | None
    surface: str | None
    outcome: str | None

    def as_kwargs(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "verb": self.verb,
            "resource_type": self.resource_type,
            "surface": self.surface,
            "outcome": self.outcome,
        }


def _usage_event_export_pages(
    app: Any,
    filters: _UsageExportFilters,
    first_page: list[UsageEvent],
) -> Iterator[list[dict[str, object]]]:
    """Yield encoded export pages, reading each later page in its own short session.

    Memory stays bounded by ``_USAGE_EXPORT_PAGE_SIZE`` rows and no database
    connection is held while the client drains a page.
    """

    page = first_page
    while page:
        yield [jsonable_encoder(event) for event in page]
        if len(page) < _USAGE_EXPORT_PAGE_SIZE:
            return
        last = page[-1]
        with app.state.db_session_factory() as session:
            page_api = app.state.session_api_factory(session, surface="http")
            page = page_api.page_usage_events(
                **filters.as_kwargs(),
                after=(last.occurred_at, last.event_id),
                limit=_USAGE_EXPORT_PAGE_SIZE,
            )


def _usage_events_jsonl_chunks(
    pages: Iterable[list[dict[str, object]]],
) -> Iterator[str]:
    for rows in pages:
        yield "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)


def _usage_events_csv_chunks(
    pages: Iterable[list[dict[str, object]]],
) -> Iterator[str]:
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_USAGE_EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for rows in pages:
        for row in rows:
            writer.writerow(row)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
    unsent = buffer.getvalue()
    if unsent:
        # No rows matched: the export is just the header line.
        yield unsent


def _ensure_admin(request: Request) -> None:
    actor = actor_from_request(request)
    if actor.role != Role.ADMIN:
        raise AuthError("Admin privileges required.")
