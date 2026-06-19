from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import JSON, String, Text, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.config import Settings
from lab_tracker.db import Base
from lab_tracker.db_models import UsageEventModel, UsageEventRollupModel
from lab_tracker.mcp_api_client import LabTrackerAPIClient, MCPSettings
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker_client.client import LabTracker


def test_usage_event_model_has_no_content_columns():
    fixed_string_columns = {
        "event_id",
        "verb",
        "resource_type",
        "resource_id",
        "actor_user_id",
        "actor_role",
        "principal_type",
        "surface",
        "project_id",
        "outcome",
    }
    for column in UsageEventModel.__table__.columns:
        assert not isinstance(column.type, (Text, JSON))
        if isinstance(column.type, String):
            assert column.name in fixed_string_columns
            assert column.type.length is not None
            assert column.type.length <= 64


def test_usage_event_seam_records_after_success_and_respects_flag():
    actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    disabled_api = _usage_api(usage_events=False)

    disabled_api.create_project("Disabled telemetry project", actor=actor)
    events, total = disabled_api.query_usage_events()
    assert events == []
    assert total == 0

    enabled_api = _usage_api(usage_events=True)
    project = enabled_api.create_project("Enabled telemetry project", actor=actor)
    events, total = enabled_api.query_usage_events()

    assert total == 1
    [event] = events
    assert event.verb == "create"
    assert event.resource_type == "project"
    assert event.resource_id == project.project_id
    assert event.project_id == project.project_id
    assert event.actor_user_id == actor.user_id
    assert event.outcome == "ok"


def test_usage_event_routes_export_search_without_query_terms(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    client.app.state.settings.usage_events = True
    headers = {**admin_auth_headers, "X-LabTracker-Surface": "cli"}

    project_response = client.post(
        "/projects",
        json={"name": "Usage telemetry project"},
        headers=headers,
    )
    assert project_response.status_code == 201
    project_id = project_response.json()["data"]["project_id"]

    question_response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "What is the hidden retention sentinel?",
            "question_type": "descriptive",
        },
        headers=headers,
    )
    assert question_response.status_code == 201
    question_id = question_response.json()["data"]["question_id"]

    note_response = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "The hidden retention sentinel appears here too.",
        },
        headers=headers,
    )
    assert note_response.status_code == 201

    view_response = client.get(f"/questions/{question_id}", headers=headers)
    assert view_response.status_code == 200

    search_response = client.get(
        "/search",
        params={"q": "hidden retention sentinel", "project_id": project_id},
        headers=headers,
    )
    assert search_response.status_code == 200

    summary_response = client.get("/usage-events/summary", headers=admin_auth_headers)
    assert summary_response.status_code == 200
    summary = summary_response.json()["data"]
    assert any(
        row["verb"] == "search"
        and row["resource_type"] == "search"
        and row["event_count"] == 1
        for row in summary
    )

    export_response = client.get(
        "/usage-events/export",
        params={"format": "jsonl"},
        headers=admin_auth_headers,
    )
    assert export_response.status_code == 200
    assert "hidden retention sentinel" not in export_response.text
    rows = [
        json.loads(line)
        for line in export_response.text.splitlines()
        if line.strip()
    ]
    search_rows = [
        row
        for row in rows
        if row["verb"] == "search" and row["resource_type"] == "search"
    ]
    assert len(search_rows) == 1
    assert search_rows[0]["result_count"] == 2
    assert search_rows[0]["surface"] == "cli"
    assert any(row["verb"] == "view" and row["resource_id"] == question_id for row in rows)

    csv_response = client.get(
        "/usage-events/export",
        params={"format": "csv"},
        headers=admin_auth_headers,
    )
    assert csv_response.status_code == 200
    assert "event_id,occurred_at,verb,resource_type" in csv_response.text
    assert "hidden retention sentinel" not in csv_response.text


def test_usage_event_retention_rolls_up_and_prunes_raw_events(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    client.app.state.settings.usage_events = True
    project_response = client.post(
        "/projects",
        json={"name": "Retention telemetry project"},
        headers=admin_auth_headers,
    )
    assert project_response.status_code == 201
    project_id = project_response.json()["data"]["project_id"]
    old_event_id = str(uuid4())

    with client.app.state.db_session_factory() as session:
        session.add(
            UsageEventModel(
                event_id=old_event_id,
                occurred_at=datetime.now(timezone.utc) - timedelta(days=366),
                verb="view",
                resource_type="project",
                resource_id=project_id,
                actor_user_id=None,
                actor_role="admin",
                principal_type="user",
                surface="http",
                project_id=project_id,
                outcome="ok",
                duration_ms=7,
                result_count=None,
            )
        )
        session.commit()

    retention_response = client.post(
        "/usage-events/retention/run",
        headers=admin_auth_headers,
    )
    assert retention_response.status_code == 200
    assert retention_response.json()["data"]["raw_events_pruned"] == 1

    with client.app.state.db_session_factory() as session:
        assert session.get(UsageEventModel, old_event_id) is None
        rollup = session.scalars(
            select(UsageEventRollupModel).where(
                UsageEventRollupModel.verb == "view",
                UsageEventRollupModel.resource_type == "project",
                UsageEventRollupModel.project_id == project_id,
            )
        ).one()
        assert rollup.event_count == 1
        assert rollup.total_duration_ms == 7


def test_mcp_api_client_sends_surface_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-LabTracker-Surface"] == "mcp"
        return httpx.Response(200, json={"data": []})

    client = LabTrackerAPIClient(
        MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.list_projects()["data"] == []
    finally:
        client.close()


def test_cli_client_sends_surface_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-LabTracker-Surface"] == "cli"
        return httpx.Response(200, json={"data": []})

    client = LabTracker(
        base_url="http://testserver",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.list_projects() == []
    finally:
        client.close()


def _usage_api(*, usage_events: bool) -> LabTrackerAPI:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = session_factory()
    settings = Settings(
        _env_file=None,
        environment="local",
        auth_enabled=False,
        usage_events=usage_events,
    )
    api = LabTrackerAPI(
        repository=SQLAlchemyLabTrackerRepository(session),
        settings=settings,
    )
    api._test_resources = (engine, session)  # type: ignore[attr-defined]
    return api
