"""HTTP integration tests for printable capture contexts."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _create_project(
    client: TestClient,
    headers: dict[str, str],
    name: str = "Capture contexts",
) -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _create_question(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
) -> str:
    response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "What was observed at the microscope?",
            "question_type": "descriptive",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["question_id"]


def _create_context(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    label: str = "Scope bench",
    default_targets: list[dict[str, str]] | None = None,
) -> dict:
    response = client.post(
        "/capture-contexts",
        json={
            "project_id": project_id,
            "label": label,
            "site_label": "Main lab",
            "place_label": "Microscope bay 2",
            "default_hint": "Scan after each rig handoff.",
            "default_targets": default_targets or [],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_capture_context_crud_returns_printable_url_and_qr(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers)

    context = _create_context(client, admin_auth_headers, project_id=project_id)

    assert context["capture_context_id"]
    assert context["capture_url"].endswith(
        f"/app/capture?context={context['capture_context_id']}"
    )
    assert context["capture_qr_svg"].startswith("<svg")
    assert 'shape-rendering="crispEdges"' in context["capture_qr_svg"]
    assert context["revoked"] is False

    listed = client.get(
        f"/capture-contexts?project_id={project_id}",
        headers=admin_auth_headers,
    )
    assert listed.status_code == 200, listed.text
    assert [item["capture_context_id"] for item in listed.json()["data"]] == [
        context["capture_context_id"]
    ]

    patched = client.patch(
        f"/capture-contexts/{context['capture_context_id']}",
        json={"label": "Updated bench", "place_label": None},
        headers=admin_auth_headers,
    )
    assert patched.status_code == 200, patched.text
    patched_context = patched.json()["data"]
    assert patched_context["label"] == "Updated bench"
    assert patched_context["place_label"] is None

    revoked = client.delete(
        f"/capture-contexts/{context['capture_context_id']}",
        headers=admin_auth_headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["data"]["revoked"] is True

    active_only = client.get(
        f"/capture-contexts?project_id={project_id}",
        headers=admin_auth_headers,
    )
    assert active_only.status_code == 200, active_only.text
    assert active_only.json()["data"] == []


def test_capture_context_note_stamps_metadata_and_default_targets(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers)
    question_id = _create_question(client, admin_auth_headers, project_id)
    context = _create_context(
        client,
        admin_auth_headers,
        project_id=project_id,
        default_targets=[{"entity_type": "question", "entity_id": question_id}],
    )

    response = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Mouse entered rig bay.",
            "metadata": {"source": "clipboard"},
            "targets": [{"entity_type": "question", "entity_id": question_id}],
            "capture_context_id": context["capture_context_id"],
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 201, response.text
    note = response.json()["data"]
    assert note["targets"] == [{"entity_type": "question", "entity_id": question_id}]
    assert note["metadata"]["source"] == "clipboard"
    assert note["metadata"]["capture_context_id"] == context["capture_context_id"]
    assert note["metadata"]["capture_context_label"] == "Scope bench"
    assert note["metadata"]["capture_site_label"] == "Main lab"
    assert note["metadata"]["capture_place_label"] == "Microscope bay 2"
    assert note["metadata"]["capture_context_hint"] == "Scan after each rig handoff."


def test_capture_context_note_rejects_revoked_or_wrong_project(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Context source")
    other_project_id = _create_project(client, admin_auth_headers, "Context target")
    context = _create_context(client, admin_auth_headers, project_id=project_id)

    mismatch = client.post(
        "/notes",
        json={
            "project_id": other_project_id,
            "raw_content": "Wrong project.",
            "capture_context_id": context["capture_context_id"],
        },
        headers=admin_auth_headers,
    )
    assert mismatch.status_code == 422, mismatch.text
    assert "note project" in mismatch.json()["error"]["message"]

    revoke = client.delete(
        f"/capture-contexts/{context['capture_context_id']}",
        headers=admin_auth_headers,
    )
    assert revoke.status_code == 200, revoke.text

    revoked = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Revoked context.",
            "capture_context_id": context["capture_context_id"],
        },
        headers=admin_auth_headers,
    )
    assert revoked.status_code == 422, revoked.text
    assert "revoked" in revoked.json()["error"]["message"]


def test_capture_context_rejects_default_targets_from_another_project(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Context project")
    other_project_id = _create_project(client, admin_auth_headers, "Other project")
    other_question_id = _create_question(
        client,
        admin_auth_headers,
        other_project_id,
    )

    create = client.post(
        "/capture-contexts",
        json={
            "project_id": project_id,
            "label": "Invalid preset",
            "default_targets": [
                {"entity_type": "question", "entity_id": other_question_id}
            ],
        },
        headers=admin_auth_headers,
    )

    assert create.status_code == 422, create.text
    assert "same project" in create.json()["error"]["message"]

    context = _create_context(client, admin_auth_headers, project_id=project_id)
    update = client.patch(
        f"/capture-contexts/{context['capture_context_id']}",
        json={
            "default_targets": [
                {"entity_type": "question", "entity_id": other_question_id}
            ]
        },
        headers=admin_auth_headers,
    )
    assert update.status_code == 422, update.text
    assert "same project" in update.json()["error"]["message"]
