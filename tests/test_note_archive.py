"""Notes can be set aside honestly: archiving always records a named reason."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/projects", json={"name": "Archive Project"}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _text_note(client: TestClient, headers: dict[str, str], project_id: str) -> str:
    response = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Whiteboard looked off today."},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["note_id"]


def test_archive_without_reviewing_defaults_to_unreviewed_reason(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _text_note(client, admin_auth_headers, project_id)

    archived = client.post(
        f"/notes/{note_id}/archive",
        headers=admin_auth_headers,
    )
    assert archived.status_code == 200
    data = archived.json()["data"]
    assert data["status"] == "archived"
    # A skipped review degrades visible coverage, never silent trust.
    assert data["archived_reason"] == "archived_unreviewed"
    assert data["archived_at"] is not None
    assert data["archived_by"] is not None


def test_archive_records_explicit_reason(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _text_note(client, admin_auth_headers, project_id)

    archived = client.post(
        f"/notes/{note_id}/archive",
        json={"reason": "reviewed_not_relevant"},
        headers=admin_auth_headers,
    )
    assert archived.status_code == 200
    data = archived.json()["data"]
    assert data["status"] == "archived"
    assert data["archived_reason"] == "reviewed_not_relevant"


def test_archive_rejects_unknown_reason(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _text_note(client, admin_auth_headers, project_id)

    response = client.post(
        f"/notes/{note_id}/archive",
        json={"reason": "because"},
        headers=admin_auth_headers,
    )
    assert response.status_code == 422


def test_create_note_cannot_start_archived_without_the_archive_command(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)

    response = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Set aside", "status": "archived"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 422
    assert "/archive" in response.json()["error"]["message"]
    listed = client.get(
        "/notes",
        params={"project_id": project_id},
        headers=admin_auth_headers,
    )
    assert listed.json()["data"] == []


def test_patch_status_archived_is_rejected_so_archiving_always_names_a_reason(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _text_note(client, admin_auth_headers, project_id)

    response = client.patch(
        f"/notes/{note_id}",
        json={"status": "archived"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 422
    assert "/archive" in response.json()["error"]["message"]
    stored = client.get(f"/notes/{note_id}", headers=admin_auth_headers).json()["data"]
    assert stored["status"] == "staged"
    assert stored["archived_reason"] is None


def test_restoring_an_archived_note_clears_its_archive_stamps(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _text_note(client, admin_auth_headers, project_id)
    archived = client.post(
        f"/notes/{note_id}/archive",
        json={"reason": "superseded"},
        headers=admin_auth_headers,
    )
    assert archived.status_code == 200

    restored = client.patch(
        f"/notes/{note_id}",
        json={"status": "staged"},
        headers=admin_auth_headers,
    )

    assert restored.status_code == 200
    data = restored.json()["data"]
    assert data["status"] == "staged"
    assert data["archived_reason"] is None
    assert data["archived_at"] is None
    assert data["archived_by"] is None
    assert data["archived_by_user_id"] is None
    stored = client.get(f"/notes/{note_id}", headers=admin_auth_headers).json()["data"]
    assert stored["status"] == "staged"
    assert stored["archived_reason"] is None
    assert stored["archived_at"] is None


def test_patching_other_fields_of_an_archived_note_keeps_its_archive_stamps(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _text_note(client, admin_auth_headers, project_id)
    archived = client.post(
        f"/notes/{note_id}/archive",
        json={"reason": "reviewed_not_relevant"},
        headers=admin_auth_headers,
    )
    assert archived.status_code == 200

    patched = client.patch(
        f"/notes/{note_id}",
        json={"status": "archived", "transcribed_text": "clarified"},
        headers=admin_auth_headers,
    )

    assert patched.status_code == 200
    data = patched.json()["data"]
    assert data["status"] == "archived"
    assert data["archived_reason"] == "reviewed_not_relevant"
    assert data["archived_at"] == archived.json()["data"]["archived_at"]
