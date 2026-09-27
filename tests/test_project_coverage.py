"""Coverage is derived, never stored: skipped review shows up as numbers, not silence."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event

from lab_tracker.coverage_query import (
    CAPTURE_SOURCE_LISTING_LIMIT,
    unreviewed_capture_counts_by_project,
)
from lab_tracker.db_models import GraphChangeOperationModel, GraphChangeSetModel
from lab_tracker.models import ProjectCoverageReport, ProjectCoverageSummary


def _project(client: TestClient, headers: dict[str, str], name: str = "Coverage project") -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _note(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    raw_content: str,
    *,
    metadata: dict[str, str] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"project_id": project_id, "raw_content": raw_content}
    if metadata is not None:
        payload["metadata"] = metadata
    response = client.post("/notes", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _timestamp(raw: object) -> datetime:
    assert isinstance(raw, str)
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _change_set(
    project_id: str,
    *,
    status: str,
    source_note_id: str,
    source_note_ids: list[str],
    clarification_requests: list[str] | None = None,
) -> GraphChangeSetModel:
    return GraphChangeSetModel(
        project_id=project_id,
        source_note_id=source_note_id,
        source_note_ids=source_note_ids,
        provider="openai",
        model="fake-gpt",
        prompt_version="coverage-test-v1",
        status=status,
        clarification_requests=clarification_requests or [],
    )


def _operation(
    change_set_id: object,
    *,
    sequence: int,
    status: str,
    source_note_ids: list[str],
) -> GraphChangeOperationModel:
    return GraphChangeOperationModel(
        change_set_id=change_set_id,
        sequence=sequence,
        op="create",
        entity_type="question",
        semantic_type="suggest_new_question",
        payload={"text": f"Operation {sequence}"},
        status=status,
        source_refs=[{"source_note_ids": source_note_ids}],
    )


def test_project_coverage_models_reject_negative_counts() -> None:
    with pytest.raises(ValidationError):
        ProjectCoverageSummary(
            project_id=uuid4(),
            unreviewed_count=-1,
            unplaced_count=0,
            archived_unreviewed_count=0,
            pending_change_sets=0,
            open_clarification_requests=0,
        )

    report = ProjectCoverageReport(
        project_id=uuid4(),
        unreviewed_count=0,
        unplaced_count=0,
        archived_unreviewed_count=0,
        pending_change_sets=0,
        open_clarification_requests=0,
    )
    assert report.capture_sources == []
    assert report.capture_sources_truncated is False
    assert report.oldest_unreviewed_at is None
    assert report.last_capture_at is None


def test_project_coverage_derives_unreviewed_unplaced_and_archived_counts(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    notes = [
        _note(client, admin_auth_headers, project_id, f"Capture {index}") for index in range(1, 9)
    ]
    n1, n2, n3, n4, n5, n6, n7, n8 = (str(note["note_id"]) for note in notes)

    with client.app.state.db_session_factory() as session:
        committed = _change_set(
            project_id, status="committed", source_note_id=n1, source_note_ids=[n1, n2]
        )
        session.add(committed)
        session.flush()
        session.add(
            _operation(committed.change_set_id, sequence=1, status="applied", source_note_ids=[n1])
        )
        session.add(
            _operation(committed.change_set_id, sequence=2, status="rejected", source_note_ids=[n2])
        )
        session.add(
            _change_set(project_id, status="rejected", source_note_id=n3, source_note_ids=[n3])
        )
        session.add(
            _change_set(project_id, status="failed", source_note_id=n7, source_note_ids=[])
        )
        session.add(
            _change_set(
                project_id,
                status="submitted",
                source_note_id=n4,
                source_note_ids=[n4],
                clarification_requests=["Which rig?", "Which day?"],
            )
        )
        session.commit()

    assert client.post(f"/notes/{n5}/archive", headers=admin_auth_headers).status_code == 200
    assert (
        client.post(
            f"/notes/{n6}/archive",
            json={"reason": "reviewed_not_relevant"},
            headers=admin_auth_headers,
        ).status_code
        == 200
    )

    response = client.get(f"/projects/{project_id}/coverage", headers=admin_auth_headers)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["project_id"] == project_id
    # N4 waits on a person, N7's draft failed, N8 was never drafted: none was reviewed.
    assert data["unreviewed_count"] == 3
    assert _timestamp(data["oldest_unreviewed_at"]) == min(
        _timestamp(notes[3]["created_at"]),
        _timestamp(notes[6]["created_at"]),
        _timestamp(notes[7]["created_at"]),
    )
    # N2 was absorbed by the committed draft but no applied operation cites it.
    assert data["unplaced_count"] == 1
    assert data["archived_unreviewed_count"] == 1
    assert data["pending_change_sets"] == 1
    assert data["open_clarification_requests"] == 2
    assert _timestamp(data["last_capture_at"]) == max(
        _timestamp(note["created_at"]) for note in notes
    )
    assert data["capture_sources_truncated"] is False
    assert [source["note_count"] for source in data["capture_sources"]] == [8]


def test_project_coverage_lists_capture_sources_last_seen(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    manual = _note(client, admin_auth_headers, project_id, "Typed by hand")
    provider_only = _note(
        client,
        admin_auth_headers,
        project_id,
        "Provider only",
        metadata={"evidence_source_provider": "git"},
    )
    rig_metadata = {
        "evidence_source_provider": "git",
        "evidence_adapter": "lt-repo",
        "capture_install_id": "A",
        "capture_host_label": "rig-1",
    }
    _note(client, admin_auth_headers, project_id, "Rig capture 1", metadata=rig_metadata)
    rig_latest = _note(
        client, admin_auth_headers, project_id, "Rig capture 2", metadata=rig_metadata
    )

    response = client.get(f"/projects/{project_id}/coverage", headers=admin_auth_headers)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["capture_sources_truncated"] is False
    sources = data["capture_sources"]
    assert [
        (
            source["evidence_source_provider"],
            source["evidence_adapter"],
            source["capture_install_id"],
            source["capture_host_label"],
            source["note_count"],
        )
        for source in sources
    ] == [
        ("git", "lt-repo", "A", "rig-1", 2),
        ("git", None, None, None, 1),
        (None, None, None, None, 1),
    ]
    assert _timestamp(sources[0]["last_capture_at"]) == _timestamp(rig_latest["created_at"])
    assert _timestamp(sources[1]["last_capture_at"]) == _timestamp(provider_only["created_at"])
    assert _timestamp(sources[2]["last_capture_at"]) == _timestamp(manual["created_at"])
    assert _timestamp(data["last_capture_at"]) == _timestamp(rig_latest["created_at"])


def test_project_coverage_capture_sources_are_bounded(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    for index in range(CAPTURE_SOURCE_LISTING_LIMIT + 1):
        _note(
            client,
            admin_auth_headers,
            project_id,
            f"Install {index}",
            metadata={"capture_install_id": f"install-{index:03d}"},
        )

    response = client.get(f"/projects/{project_id}/coverage", headers=admin_auth_headers)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data["capture_sources"]) == CAPTURE_SOURCE_LISTING_LIMIT
    assert data["capture_sources_truncated"] is True
    assert data["unreviewed_count"] == CAPTURE_SOURCE_LISTING_LIMIT + 1


def test_unreviewed_capture_counts_by_project_batches_projects(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    first = _project(client, admin_auth_headers, "First")
    second = _project(client, admin_auth_headers, "Second")
    empty = _project(client, admin_auth_headers, "Empty")
    for index in range(2):
        _note(client, admin_auth_headers, first, f"First {index}")
    _note(client, admin_auth_headers, second, "Second 0")

    statements = 0

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        nonlocal statements
        statements += 1

    with client.app.state.db_session_factory() as session:
        counts = unreviewed_capture_counts_by_project(session, [first, second, empty])
        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", before_cursor_execute)
        try:
            nothing = unreviewed_capture_counts_by_project(session, [])
        finally:
            event.remove(engine, "before_cursor_execute", before_cursor_execute)

    assert isinstance(counts, defaultdict)
    assert dict(counts) == {first: 2, second: 1}
    assert counts[empty] == 0
    assert dict(nothing) == {}
    assert statements == 0


def test_project_coverage_requires_project_read_and_records_view_usage(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
) -> None:
    client.app.state.settings.usage_events = True
    visible = scoped_project_member.visible_project_id
    hidden = scoped_project_member.hidden_project_id
    member_headers = scoped_project_member.member_headers

    authorized = client.get(f"/projects/{visible}/coverage", headers=member_headers)
    assert authorized.status_code == 200, authorized.text
    assert authorized.json()["data"]["project_id"] == visible

    denied = client.get(f"/projects/{hidden}/coverage", headers=member_headers)
    missing = client.get(f"/projects/{uuid4()}/coverage", headers=member_headers)
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json()
    assert denied.json()["error"]["message"] == "Project does not exist."

    export = client.get(
        "/usage-events/export",
        params={"format": "jsonl"},
        headers=admin_auth_headers,
    )
    assert export.status_code == 200
    rows = [json.loads(line) for line in export.text.splitlines() if line.strip()]
    views = [row for row in rows if row["verb"] == "view" and row["resource_type"] == "project"]
    assert [row["resource_id"] for row in views] == [visible]


def test_project_coverage_rejects_unauthenticated(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)

    response = client.get(f"/projects/{project_id}/coverage")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_error"
