from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from api_helpers import (
    TEST_STORE_AUTHORITY_GRANT_ID,
    ExactCandidateTestStoreAuthority,
    register_test_resources,
)
from fastapi.testclient import TestClient
from sqlalchemy import JSON, String, Text, create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.config import Settings
from lab_tracker.db import Base
from lab_tracker.db_models import UsageEventModel, UsageEventRollupModel
from lab_tracker.mcp_api_client import LabTrackerAPIClient, MCPSettings
from lab_tracker.models import StoreKind
from lab_tracker.services import base as service_base
from lab_tracker.services.evidence_bundle_service import (
    CreateSourceNoteIntent,
    RecordEvidenceBundleCommand,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker.sqlalchemy_repository_parts.usage import rollup_usage_events_before
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


def test_data_store_registration_records_only_safe_generic_usage_identity() -> None:
    actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    api = _usage_api(usage_events=True)
    authority = ExactCandidateTestStoreAuthority()
    api._store_authority_registry = authority
    api.data_stores.store_authority_registry = authority
    project = api.create_project("Store telemetry project", actor=actor)

    store = api.create_data_store(
        project_id=project.project_id,
        name="safe-audit-store",
        kind=StoreKind.HTTP,
        root="https://sensitive-target.example.test/private",
        credential_ref=None,
        authority_grant_id=TEST_STORE_AUTHORITY_GRANT_ID,
        actor=actor,
    )

    events, total = api.query_usage_events()
    store_events = [event for event in events if event.resource_type == "data_store"]
    assert total == 2
    assert len(store_events) == 1
    [event] = store_events
    assert event.verb == "create"
    assert event.resource_id == store.store_id
    assert event.project_id == project.project_id
    assert event.actor_user_id == actor.user_id
    assert event.outcome == "ok"


def test_direct_usage_event_failure_is_fail_soft_and_session_remains_reusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _usage_api(usage_events=True)
    actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    usage_events = api.projects.repository.usage_events
    original_save = usage_events.save
    warnings: list[tuple[str, tuple[object, ...]]] = []

    def fail_save(_event) -> None:  # noqa: ANN001
        raise RuntimeError("telemetry storage unavailable")

    def record_warning(message: str, *args: object, **_kwargs: object) -> None:
        warnings.append((message, args))

    monkeypatch.setattr(usage_events, "save", fail_save)
    monkeypatch.setattr(service_base._logger, "warning", record_warning)  # noqa: SLF001

    first = api.create_project("Business write survives telemetry failure", actor=actor)

    assert api.get_project(first.project_id) == first
    assert warnings
    assert warnings[0][0] == "Deferred %s action failed: %s"
    assert warnings[0][1][0] == "after_commit"
    monkeypatch.setattr(usage_events, "save", original_save)

    second = api.create_project("Session remains reusable", actor=actor)
    events, total = api.query_usage_events()

    assert api.get_project(second.project_id) == second
    assert total == 1
    assert events[0].resource_id == second.project_id


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
        row["verb"] == "search" and row["resource_type"] == "search" and row["event_count"] == 1
        for row in summary
    )

    export_response = client.get(
        "/usage-events/export",
        params={"format": "jsonl"},
        headers=admin_auth_headers,
    )
    assert export_response.status_code == 200
    assert "hidden retention sentinel" not in export_response.text
    rows = [json.loads(line) for line in export_response.text.splitlines() if line.strip()]
    search_rows = [
        row for row in rows if row["verb"] == "search" and row["resource_type"] == "search"
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


def test_project_delete_usage_event_survives_the_cascade_delete(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lab_tracker import api as api_module

    warnings: list[str] = []

    def record_warning(message: str, *args: object, **_kwargs: object) -> None:
        warnings.append(message % args if args else message)

    monkeypatch.setattr(api_module._logger, "warning", record_warning)  # noqa: SLF001
    monkeypatch.setattr(service_base._logger, "warning", record_warning)  # noqa: SLF001
    client.app.state.settings.usage_events = True

    project_response = client.post(
        "/projects",
        json={"name": "Project deleted with telemetry"},
        headers=admin_auth_headers,
    )
    assert project_response.status_code == 201
    project_id = project_response.json()["data"]["project_id"]

    delete_response = client.delete(f"/projects/{project_id}", headers=admin_auth_headers)
    assert delete_response.status_code == 200

    with client.app.state.db_session_factory() as session:
        delete_events = list(
            session.scalars(
                select(UsageEventModel).where(
                    UsageEventModel.verb == "delete",
                    UsageEventModel.resource_type == "project",
                )
            )
        )
    assert not [message for message in warnings if "after_commit" in message], warnings
    assert len(delete_events) == 1
    [event] = delete_events
    assert str(event.resource_id) == project_id
    assert event.project_id is None
    assert event.outcome == "ok"


def _seed_export_usage_events(client: TestClient, count: int) -> list[str]:
    """Insert usage events, several sharing one timestamp, and return export order."""

    base_time = datetime.now(timezone.utc) - timedelta(days=1)
    rows = [
        UsageEventModel(
            event_id=str(uuid4()),
            # Groups of three share an occurred_at so pages must break ties by id.
            occurred_at=base_time + timedelta(seconds=index // 3),
            verb="view",
            resource_type="question",
            resource_id=str(uuid4()),
            actor_user_id=None,
            actor_role="admin",
            principal_type="user",
            surface="http",
            project_id=None,
            outcome="ok",
            duration_ms=index,
            result_count=None,
        )
        for index in range(count)
    ]
    ordered = sorted(
        ((row.occurred_at, str(row.event_id)) for row in rows),
        reverse=True,
    )
    with client.app.state.db_session_factory() as session:
        session.add_all(rows)
        session.commit()
    return [event_id for _occurred_at, event_id in ordered]


def test_usage_event_export_streams_bounded_pages_in_stable_order(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lab_tracker.routes import usage_events as usage_routes
    from lab_tracker.sqlalchemy_repository_parts.usage import SQLAlchemyUsageEventRepository

    page_size = 4
    monkeypatch.setattr(usage_routes, "_USAGE_EXPORT_PAGE_SIZE", page_size, raising=False)
    fetch_limits: list[int | None] = []
    for method_name in ("query", "query_page"):
        original = getattr(SQLAlchemyUsageEventRepository, method_name, None)
        if original is None:
            continue

        def spy(self, *args, _original=original, **kwargs):  # noqa: ANN001, ANN002, ANN003
            fetch_limits.append(kwargs.get("limit"))
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(SQLAlchemyUsageEventRepository, method_name, spy)

    expected_ids = _seed_export_usage_events(client, 11)

    jsonl_response = client.get(
        "/usage-events/export",
        params={"format": "jsonl", "resource_type": "question"},
        headers=admin_auth_headers,
    )
    assert jsonl_response.status_code == 200, jsonl_response.text
    assert jsonl_response.headers["content-type"].startswith("application/x-ndjson")
    assert jsonl_response.headers["content-disposition"] == (
        'attachment; filename="usage-events.jsonl"'
    )
    assert jsonl_response.text.endswith("\n")
    rows = [json.loads(line) for line in jsonl_response.text.splitlines()]
    assert [row["event_id"] for row in rows] == expected_ids
    assert fetch_limits, "export must read usage events through bounded pages"
    assert all(limit is not None and limit <= page_size for limit in fetch_limits), fetch_limits

    fetch_limits.clear()
    csv_response = client.get(
        "/usage-events/export",
        params={"format": "csv", "resource_type": "question"},
        headers=admin_auth_headers,
    )
    assert csv_response.status_code == 200, csv_response.text
    assert csv_response.headers["content-type"].startswith("text/csv")
    csv_lines = csv_response.text.split("\r\n")
    assert csv_lines[0] == (
        "event_id,occurred_at,verb,resource_type,resource_id,actor_user_id,actor_role,"
        "principal_type,surface,project_id,outcome,duration_ms,result_count"
    )
    assert csv_lines[-1] == ""
    assert [line.split(",", 1)[0] for line in csv_lines[1:-1]] == expected_ids
    assert all(limit is not None and limit <= page_size for limit in fetch_limits), fetch_limits

    empty_params = {"resource_type": "question", "verb": "delete"}
    empty_csv = client.get(
        "/usage-events/export",
        params={**empty_params, "format": "csv"},
        headers=admin_auth_headers,
    )
    assert empty_csv.status_code == 200
    assert empty_csv.text == csv_lines[0] + "\r\n"
    empty_jsonl = client.get(
        "/usage-events/export",
        params={**empty_params, "format": "jsonl"},
        headers=admin_auth_headers,
    )
    assert empty_jsonl.status_code == 200
    assert empty_jsonl.text == ""


def test_usage_event_export_pages_do_not_repeat_rows_when_events_arrive_mid_export(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lab_tracker.routes import usage_events as usage_routes
    from lab_tracker.sqlalchemy_repository_parts.usage import SQLAlchemyUsageEventRepository

    monkeypatch.setattr(usage_routes, "_USAGE_EXPORT_PAGE_SIZE", 3)
    expected_ids = _seed_export_usage_events(client, 8)
    original = SQLAlchemyUsageEventRepository.query_page
    pages_served = 0

    def insert_newer_events_after_first_page(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        nonlocal pages_served
        page = original(self, *args, **kwargs)
        pages_served += 1
        if pages_served == 1:
            _seed_export_usage_events(client, 5)
        return page

    monkeypatch.setattr(
        SQLAlchemyUsageEventRepository,
        "query_page",
        insert_newer_events_after_first_page,
    )
    # New events are timestamped a day ago + a few seconds, i.e. at the same times
    # as the seeded rows; keyset paging must still neither repeat nor skip rows
    # that existed when their page was read.
    response = client.get(
        "/usage-events/export",
        params={"format": "jsonl", "resource_type": "question"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    exported = [json.loads(line)["event_id"] for line in response.text.splitlines()]
    assert len(exported) == len(set(exported))
    assert set(expected_ids) <= set(exported)
    assert pages_served > 1


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


def test_usage_event_retention_merges_existing_null_dimension_rollup(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    client.app.state.settings.usage_events = True
    occurred_at = datetime.now(timezone.utc) - timedelta(days=366)

    for duration_ms, result_count in ((7, 2), (4, 5)):
        with client.app.state.db_session_factory() as session:
            session.add(
                UsageEventModel(
                    event_id=str(uuid4()),
                    occurred_at=occurred_at,
                    verb="view",
                    resource_type="usage_event",
                    resource_id=None,
                    actor_user_id=None,
                    actor_role=None,
                    principal_type=None,
                    surface=None,
                    project_id=None,
                    outcome="ok",
                    duration_ms=duration_ms,
                    result_count=result_count,
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
        rollups = list(
            session.scalars(
                select(UsageEventRollupModel).where(
                    UsageEventRollupModel.verb == "view",
                    UsageEventRollupModel.resource_type == "usage_event",
                    UsageEventRollupModel.project_id.is_(None),
                    UsageEventRollupModel.actor_role.is_(None),
                    UsageEventRollupModel.principal_type.is_(None),
                    UsageEventRollupModel.surface.is_(None),
                )
            )
        )
        assert len(rollups) == 1
        [rollup] = rollups
        assert rollup.event_count == 2
        assert rollup.total_duration_ms == 11
        assert rollup.total_result_count == 7


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
    register_test_resources(engine, session)
    return api


def _usage_rows(client: TestClient) -> list[UsageEventModel]:
    with client.app.state.db_session_factory() as session:
        rows = session.scalars(select(UsageEventModel)).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def _new_usage_rows(
    client: TestClient,
    before: list[UsageEventModel],
) -> list[UsageEventModel]:
    seen = {str(row.event_id) for row in before}
    return [row for row in _usage_rows(client) if str(row.event_id) not in seen]


def test_collection_snapshot_capture_records_exactly_one_usage_event(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    client.app.state.settings.usage_events = True
    project = client.post(
        "/projects",
        json={"name": "Collection telemetry"},
        headers=admin_auth_headers,
    )
    assert project.status_code == 201
    session = client.post(
        "/sessions",
        json={
            "project_id": project.json()["data"]["project_id"],
            "session_type": "operational",
        },
        headers=admin_auth_headers,
    )
    assert session.status_code == 201
    session_id = session.json()["data"]["session_id"]
    before = _usage_rows(client)

    response = client.post(
        f"/sessions/{session_id}/collections/trials/snapshots",
        json={
            "client_capture_id": "capture-telemetry-1",
            "observed_at": "2026-09-21T12:00:00Z",
            "complete": True,
            "manifest": {
                "schema_version": 1,
                "members": [{"path": "trial-0001/data.bin", "checksum": "a" * 64, "size_bytes": 3}],
            },
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 201, response.text
    [event] = _new_usage_rows(client, before)
    assert event.verb == "create"
    assert event.resource_type == "acquisition_collection"
    assert str(event.resource_id) == response.json()["data"]["collection_id"]
    assert event.outcome == "ok"


@pytest.mark.parametrize(
    ("dry_run", "expected_status", "expected_verb"),
    [(False, 201, "create"), (True, 200, "view")],
)
def test_evidence_bundle_records_exactly_one_usage_event(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    dry_run: bool,
    expected_status: int,
    expected_verb: str,
) -> None:
    client.app.state.settings.usage_events = True
    project = client.post(
        "/projects",
        json={"name": "Bundle telemetry"},
        headers=admin_auth_headers,
    )
    assert project.status_code == 201
    project_id = project.json()["data"]["project_id"]
    before = _usage_rows(client)

    response = client.post(
        "/evidence-bundles",
        json={
            "project_id": project_id,
            "source_note": {"kind": "create", "raw_content": "Telemetry evidence"},
            "dry_run": dry_run,
            "idempotency_key": "bundle-telemetry-key",
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == expected_status, response.text
    [event] = _new_usage_rows(client, before)
    assert event.verb == expected_verb
    assert event.resource_type == "evidence_bundle"
    assert str(event.project_id) == project_id
    assert event.outcome == "ok"


def test_failed_evidence_bundle_records_one_error_usage_event(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    client.app.state.settings.usage_events = True
    project = client.post(
        "/projects",
        json={"name": "Bundle error telemetry"},
        headers=admin_auth_headers,
    )
    assert project.status_code == 201
    project_id = project.json()["data"]["project_id"]
    before = _usage_rows(client)

    response = client.post(
        "/evidence-bundles",
        json={
            "project_id": project_id,
            "source_note": {"kind": "existing", "note_id": str(uuid4())},
            "dry_run": False,
            "idempotency_key": "bundle-telemetry-missing-note",
        },
        headers=admin_auth_headers,
    )

    assert response.status_code >= 400, response.text
    [event] = _new_usage_rows(client, before)
    assert event.verb == "create"
    assert event.resource_type == "evidence_bundle"
    assert str(event.project_id) == project_id
    assert event.outcome == "error"


def test_direct_evidence_bundle_call_records_usage_without_nesting_its_transaction() -> None:
    api = _usage_api(usage_events=True)
    actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    project = api.create_project("Direct bundle telemetry", actor=actor)

    result = api.record_evidence_bundle(
        RecordEvidenceBundleCommand(
            project_id=project.project_id,
            source_note=CreateSourceNoteIntent(raw_content="Direct evidence"),
            dry_run=False,
            idempotency_key="direct-bundle-key",
        ),
        actor=actor,
    )

    assert result.outcome == "created"
    events, _total = api.query_usage_events()
    bundle_events = [event for event in events if event.resource_type == "evidence_bundle"]
    assert len(bundle_events) == 1
    assert bundle_events[0].verb == "create"
    assert bundle_events[0].project_id == project.project_id


def test_usage_event_rollup_reads_existing_rollups_once_and_streams_bucket_columns():
    """L118: retention must not issue one rollup SELECT per bucket or hydrate full rows."""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    register_test_resources(engine, session)
    old = datetime(2024, 3, 1, 12, tzinfo=timezone.utc)
    existing_day = old.date()
    session.add(
        UsageEventRollupModel(
            rollup_id=str(uuid4()),
            day=existing_day,
            verb="view",
            resource_type="project",
            project_id=None,
            actor_role=None,
            principal_type=None,
            surface=None,
            outcome="ok",
            event_count=3,
            total_duration_ms=30,
            total_result_count=0,
        )
    )
    for day_offset, verb in ((0, "view"), (0, "view"), (0, "search"), (1, "view"), (2, "export")):
        session.add(
            UsageEventModel(
                event_id=str(uuid4()),
                occurred_at=old + timedelta(days=day_offset),
                verb=verb,
                resource_type="project",
                resource_id=None,
                actor_user_id=None,
                actor_role=None,
                principal_type=None,
                surface=None,
                project_id=None,
                outcome="ok",
                duration_ms=5,
                result_count=1,
            )
        )
    session.add(
        UsageEventModel(
            event_id=str(uuid4()),
            occurred_at=datetime.now(timezone.utc),
            verb="view",
            resource_type="project",
            outcome="ok",
        )
    )
    session.commit()

    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        pruned = rollup_usage_events_before(session, old + timedelta(days=30))
        session.commit()
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert pruned == 5
    rollup_reads = [
        item
        for item in statements
        if item.startswith("SELECT") and "FROM usage_event_rollups" in item
    ]
    assert len(rollup_reads) == 1
    event_reads = [
        item for item in statements if item.startswith("SELECT") and "FROM usage_events" in item
    ]
    assert event_reads
    assert all("usage_events.resource_id" not in item for item in event_reads)
    rollups = {
        (row.day, row.verb): (row.event_count, row.total_duration_ms, row.total_result_count)
        for row in session.scalars(select(UsageEventRollupModel))
    }
    assert rollups == {
        (existing_day, "view"): (5, 40, 2),
        (existing_day, "search"): (1, 5, 1),
        (existing_day + timedelta(days=1), "view"): (1, 5, 1),
        (existing_day + timedelta(days=2), "export"): (1, 5, 1),
    }


def _service_token_headers(
    client: TestClient, admin_auth_headers: dict[str, str], project_id: str
) -> dict:
    # A personal token uses its own role, so its user needs project membership.
    user_id = client.get("/auth/me", headers=admin_auth_headers).json()["data"]["user_id"]
    membership = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": user_id, "role": "viewer"},
        headers=admin_auth_headers,
    )
    assert membership.status_code == 201, membership.text
    response = client.post(
        "/auth/tokens",
        json={
            "label": "Consulting agent",
            "role": "editor",
            "read_only": True,
            "scope": "all",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        },
        headers=admin_auth_headers,
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['data']['secret']}"}


def test_decision_context_consultation_records_a_content_free_view_event(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    client.app.state.settings.usage_events = True
    project = client.post(
        "/projects", json={"name": "Consultation telemetry"}, headers=admin_auth_headers
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["data"]["project_id"]
    headers = _service_token_headers(client, admin_auth_headers, project_id)
    query = "distinctive-consultation-query-7f3a"
    before = _usage_rows(client)

    response = client.post(
        "/assistant/decision-context",
        json={"task_kind": "summary", "query": query, "project_id": project_id},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    [event] = _new_usage_rows(client, before)
    assert event.verb == "view"
    assert event.resource_type == "decision_context"
    assert len(event.resource_type) <= 40
    assert event.principal_type == "service"
    assert str(event.project_id) == project_id
    assert event.resource_id is None
    assert event.outcome == "ok"
    row_text = " ".join(
        str(getattr(event, column.name)) for column in UsageEventModel.__table__.columns
    )
    assert query not in row_text
    assert "summary" not in row_text


def test_failed_decision_context_records_no_event(
    client: TestClient,
    scoped_project_member,
) -> None:
    client.app.state.settings.usage_events = True
    before = _usage_rows(client)

    response = client.post(
        "/assistant/decision-context",
        json={
            "task_kind": "summary",
            "query": "hidden project",
            "project_id": scoped_project_member.hidden_project_id,
        },
        headers=scoped_project_member.member_headers,
    )

    assert response.status_code == 403, response.text
    assert _new_usage_rows(client, before) == []
