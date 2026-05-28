"""Tests for the daily-batch graph context builder (lab-tracker-jdy)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi.testclient import TestClient

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import Note
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


@contextmanager
def _request_api(client: TestClient) -> Iterator[LabTrackerAPI]:
    session = client.app.state.db_session_factory()
    try:
        repository = SQLAlchemyLabTrackerRepository(session)
        api = client.app.state.lab_tracker_api.for_request(repository)
        yield api
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _create_project(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _create_question(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    text: str,
    status: str = "active",
) -> dict[str, str]:
    response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": text,
            "question_type": "descriptive",
            "status": status,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _quick_capture(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    filename: str,
    body: bytes,
    content_type: str,
) -> str:
    response = client.post(
        "/notes/quick-capture",
        data={"project_id": project_id},
        files={"file": (filename, body, content_type)},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    return response.json()["data"]["note_id"]


def _load_notes(client: TestClient, note_ids: list[str]) -> list[Note]:
    with _request_api(client) as api:
        return [api.get_note(UUID(note_id)) for note_id in note_ids]


def test_batch_context_groups_per_project_with_questions_and_recent_neighborhood(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Plume Navigation")
    active_question = _create_question(
        client,
        admin_auth_headers,
        project_id=project_id,
        text="How do plume statistics shape navigation?",
        status="active",
    )
    staged_question = _create_question(
        client,
        admin_auth_headers,
        project_id=project_id,
        text="Are temporal gradients sufficient cue?",
        status="staged",
    )
    older_note_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_id,
        filename="prior.txt",
        body=b"earlier observation",
        content_type="text/plain",
    )
    note_a_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_id,
        filename="rig2-fly12.jpg",
        body=b"fake-image-bytes-1",
        content_type="image/jpeg",
    )
    note_b_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_id,
        filename="rig2-fly13.jpg",
        body=b"fake-image-bytes-2",
        content_type="image/jpeg",
    )

    batch_notes = _load_notes(client, [note_a_id, note_b_id])

    with _request_api(client) as api:
        packet = api.build_batch_graph_context(batch_notes)

    assert packet["mode"] == "graph_batch"
    assert packet["batch_window"] is None
    assert packet["current_user"] is None
    assert packet["truncated_note_count"] == 0
    assert {note["id"] for note in packet["batch_notes"]} == {note_a_id, note_b_id}
    assert {artifact["note_id"] for artifact in packet["source_artifacts"]} == {
        note_a_id,
        note_b_id,
    }
    assert all(artifact["type"] == "image" for artifact in packet["source_artifacts"])

    assert len(packet["projects"]) == 1
    project_block = packet["projects"][0]
    assert project_block["id"] == project_id
    assert project_block["label"] == "Plume Navigation"
    assert set(project_block["note_ids_in_batch"]) == {note_a_id, note_b_id}
    question_ids_in_context = {q["id"] for q in project_block["active_or_staged_questions"]}
    assert question_ids_in_context == {
        active_question["question_id"],
        staged_question["question_id"],
    }
    recent_note_ids = {n["id"] for n in project_block["recent_notes"]}
    assert older_note_id in recent_note_ids
    assert note_a_id not in recent_note_ids
    assert note_b_id not in recent_note_ids

    summary = packet["context_summary"]
    assert summary["counts"]["projects"] == 1
    assert summary["counts"]["batch_notes"] == 2
    assert summary["counts"]["active_or_staged_questions"] == 2
    assert summary["counts"]["recent_notes"] >= 1
    assert summary["source_artifact_counts"] == {"image": 2}
    assert summary["truncated_note_count"] == 0


def test_batch_context_spans_multiple_projects_with_independent_blocks(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_a = _create_project(client, admin_auth_headers, "Project A")
    project_b = _create_project(client, admin_auth_headers, "Project B")
    _create_question(
        client, admin_auth_headers, project_id=project_a, text="Question for A"
    )
    _create_question(
        client, admin_auth_headers, project_id=project_b, text="Question for B"
    )
    note_a_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_a,
        filename="a.txt",
        body=b"in A",
        content_type="text/plain",
    )
    note_b_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_b,
        filename="b.txt",
        body=b"in B",
        content_type="text/plain",
    )

    batch_notes = _load_notes(client, [note_a_id, note_b_id])

    with _request_api(client) as api:
        packet = api.build_batch_graph_context(batch_notes)

    blocks_by_project = {block["id"]: block for block in packet["projects"]}
    assert set(blocks_by_project) == {project_a, project_b}
    assert blocks_by_project[project_a]["note_ids_in_batch"] == [note_a_id]
    assert blocks_by_project[project_b]["note_ids_in_batch"] == [note_b_id]
    a_questions = {q["label"] for q in blocks_by_project[project_a]["active_or_staged_questions"]}
    b_questions = {q["label"] for q in blocks_by_project[project_b]["active_or_staged_questions"]}
    assert a_questions == {"Question for A"}
    assert b_questions == {"Question for B"}


def test_batch_context_carries_window_and_actor_metadata(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    from lab_tracker.auth import AuthContext, Role

    project_id = _create_project(client, admin_auth_headers, "Windowed")
    note_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_id,
        filename="snap.jpg",
        body=b"image",
        content_type="image/jpeg",
    )
    batch_notes = _load_notes(client, [note_id])
    until = datetime(2026, 5, 22, 6, 0, tzinfo=timezone.utc)
    since = until - timedelta(days=1)
    actor = AuthContext(
        user_id=UUID("00000000-0000-0000-0000-000000000123"),
        role=Role.ADMIN,
    )

    with _request_api(client) as api:
        packet = api.build_batch_graph_context(
            batch_notes,
            window=(since, until),
            actor=actor,
        )

    assert packet["batch_window"] == {
        "since": since.isoformat(),
        "until": until.isoformat(),
    }
    assert packet["current_user"] == {
        "id": "00000000-0000-0000-0000-000000000123",
        "role": "admin",
    }


def test_batch_context_truncates_overflow_and_reports_count(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch,
):
    from lab_tracker.services import graph_draft_service

    monkeypatch.setattr(graph_draft_service, "_BATCH_NOTE_LIMIT", 3)

    project_id = _create_project(client, admin_auth_headers, "Truncation")
    note_ids: list[str] = []
    for index in range(5):
        note_ids.append(
            _quick_capture(
                client,
                admin_auth_headers,
                project_id=project_id,
                filename=f"snap-{index}.jpg",
                body=f"bytes-{index}".encode(),
                content_type="image/jpeg",
            )
        )
    batch_notes = _load_notes(client, note_ids)

    with _request_api(client) as api:
        packet = api.build_batch_graph_context(batch_notes)

    assert packet["truncated_note_count"] == 2
    assert len(packet["batch_notes"]) == 3
    assert packet["context_summary"]["counts"]["batch_notes"] == 3
    assert any(
        "batch truncated" in warning
        for warning in packet["context_summary"]["warnings"]
    )


def test_batch_context_handles_empty_batch(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    with _request_api(client) as api:
        packet = api.build_batch_graph_context([])

    assert packet["mode"] == "graph_batch"
    assert packet["batch_notes"] == []
    assert packet["projects"] == []
    assert packet["truncated_note_count"] == 0
    summary = packet["context_summary"]
    assert summary["counts"]["projects"] == 0
    assert summary["counts"]["batch_notes"] == 0
    assert any(
        warning == "no source artifacts were included"
        for warning in summary["warnings"]
    )
    # Sanity-check serializability of the empty packet.
    json.dumps(packet, sort_keys=True, default=str)
