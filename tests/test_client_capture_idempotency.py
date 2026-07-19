"""Server-side client_capture_id idempotency for projects and questions.

Mirrors the notes precedent so a consumer get_or_create_* helper can pass a
deterministic key and have concurrent identical creates resolve to one canonical
entity (lab-tracker-ggzs.4).
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _project(client: TestClient, headers: dict[str, str], **body) -> dict:
    response = client.post("/projects", json=body, headers=headers)
    assert response.status_code in (200, 201), response.text
    return response.json()["data"]


def test_create_project_is_idempotent_by_client_capture_id(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    first = _project(
        client,
        admin_auth_headers,
        name="Idempotent Project",
        client_capture_id="cap-project-1",
    )
    project_id = first["project_id"]

    # Re-issuing the same capture id returns the same project (idempotent),
    # even with different other fields — one canonical entity, not a duplicate.
    repeat = _project(
        client,
        admin_auth_headers,
        name="A Different Name",
        client_capture_id="cap-project-1",
    )
    assert repeat["project_id"] == project_id

    # A different capture id creates a distinct project.
    other = _project(
        client,
        admin_auth_headers,
        name="Idempotent Project",
        client_capture_id="cap-project-2",
    )
    assert other["project_id"] != project_id

    # No capture id at all always creates (same-named projects are legitimate).
    bare = _project(client, admin_auth_headers, name="Idempotent Project")
    assert bare["project_id"] not in {project_id, other["project_id"]}


def test_create_question_is_idempotent_by_client_capture_id(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project = _project(client, admin_auth_headers, name="Question Project")
    project_id = project["project_id"]

    first = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does the key dedupe?",
            "question_type": "descriptive",
            "client_capture_id": "cap-question-1",
        },
        headers=admin_auth_headers,
    )
    assert first.status_code in (200, 201), first.text
    question_id = first.json()["data"]["question_id"]

    repeat = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "A different question?",
            "question_type": "descriptive",
            "client_capture_id": "cap-question-1",
        },
        headers=admin_auth_headers,
    )
    assert repeat.json()["data"]["question_id"] == question_id

    other = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does the key dedupe?",
            "question_type": "descriptive",
            "client_capture_id": "cap-question-2",
        },
        headers=admin_auth_headers,
    )
    assert other.json()["data"]["question_id"] != question_id
