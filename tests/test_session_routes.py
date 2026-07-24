from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from lab_tracker.models import encode_session_link_code


def _create_project(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _create_question(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    *,
    text: str = "Which session route should be exercised?",
) -> str:
    response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": text,
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["question_id"]


def _create_operational_session(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
) -> dict[str, Any]:
    response = client.post(
        "/sessions",
        json={"project_id": project_id, "session_type": "operational"},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]


def test_get_session_by_link_is_opaque_to_outsiders(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
    viewer_user,
) -> None:
    session = _create_operational_session(
        client,
        admin_auth_headers,
        scoped_project_member.visible_project_id,
    )

    allowed = client.get(
        f"/sessions/by-link/{session['link_code']}",
        headers=scoped_project_member.member_headers,
    )
    denied = client.get(
        f"/sessions/by-link/{session['link_code']}",
        headers=viewer_user.headers,
    )
    missing = client.get(
        f"/sessions/by-link/{encode_session_link_code(uuid4())}",
        headers=viewer_user.headers,
    )

    assert allowed.status_code == 200
    assert allowed.json()["data"]["session_id"] == session["session_id"]
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json() == {
        "error": {
            "code": "not_found",
            "message": "Session does not exist.",
            "issues": None,
        }
    }


def test_promote_operational_session_to_scientific_over_http(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _create_project(client, admin_auth_headers, "HTTP session promote")
    question_id = _create_question(client, admin_auth_headers, project_id)
    session = _create_operational_session(client, admin_auth_headers, project_id)

    response = client.post(
        f"/sessions/{session['session_id']}/promote",
        json={"primary_question_id": question_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["session_id"] == session["session_id"]
    assert payload["session_type"] == "scientific"
    assert payload["primary_question_id"] == question_id


def test_promote_operational_session_to_dataset_over_http(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _create_project(client, admin_auth_headers, "HTTP session dataset")
    question_id = _create_question(client, admin_auth_headers, project_id)
    session = _create_operational_session(client, admin_auth_headers, project_id)
    output = client.post(
        f"/sessions/{session['session_id']}/outputs",
        json={
            "file_path": "sessions/run-001.nwb",
            "checksum": "sha256:session-output",
            "size_bytes": 128,
        },
        headers=admin_auth_headers,
    )
    assert output.status_code == 201

    response = client.post(
        f"/sessions/{session['session_id']}/promote-to-dataset",
        json={
            "primary_question_id": question_id,
            "commit_manifest": {"metadata": {"source": "session-route-test"}},
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    dataset = response.json()["data"]
    assert dataset["project_id"] == project_id
    assert dataset["status"] == "committed"
    assert dataset["commit_manifest"]["source_session_id"] == session["session_id"]
    assert dataset["commit_manifest"]["metadata"] == {"source": "session-route-test"}
    assert dataset["commit_manifest"]["files"] == [
        {
            "file_id": None,
            "path": "sessions/run-001.nwb",
            "checksum": "sha256:session-output",
            "size_bytes": 128,
        }
    ]
    reloaded = client.get(
        f"/datasets/{dataset['dataset_id']}",
        headers=admin_auth_headers,
    )
    assert reloaded.status_code == 200
    assert (
        reloaded.json()["data"]["commit_manifest"]["files"] == dataset["commit_manifest"]["files"]
    )


def test_delete_session_rejects_promoted_dataset_source(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _create_project(client, admin_auth_headers, "HTTP session source guard")
    question_id = _create_question(client, admin_auth_headers, project_id)
    session = _create_operational_session(client, admin_auth_headers, project_id)
    output = client.post(
        f"/sessions/{session['session_id']}/outputs",
        json={
            "file_path": "sessions/guarded-run.nwb",
            "checksum": "sha256:guarded-session-output",
        },
        headers=admin_auth_headers,
    )
    assert output.status_code == 201
    promoted = client.post(
        f"/sessions/{session['session_id']}/promote-to-dataset",
        json={"primary_question_id": question_id},
        headers=admin_auth_headers,
    )
    assert promoted.status_code == 201
    assert promoted.json()["data"]["status"] == "committed"

    delete_response = client.delete(
        f"/sessions/{session['session_id']}",
        headers=admin_auth_headers,
    )
    lookup_response = client.get(
        f"/sessions/{session['session_id']}",
        headers=admin_auth_headers,
    )

    assert delete_response.status_code == 422
    assert "non-staged datasets reference" in delete_response.json()["error"]["message"]
    assert lookup_response.status_code == 200


def test_close_and_delete_session_over_http(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _create_project(client, admin_auth_headers, "HTTP close delete")
    session_to_close = _create_operational_session(client, admin_auth_headers, project_id)
    session_to_delete = _create_operational_session(client, admin_auth_headers, project_id)

    close_response = client.patch(
        f"/sessions/{session_to_close['session_id']}",
        json={"status": "closed"},
        headers=admin_auth_headers,
    )
    delete_response = client.delete(
        f"/sessions/{session_to_delete['session_id']}",
        headers=admin_auth_headers,
    )
    deleted_lookup = client.get(
        f"/sessions/{session_to_delete['session_id']}",
        headers=admin_auth_headers,
    )

    assert close_response.status_code == 200
    closed = close_response.json()["data"]
    assert closed["status"] == "closed"
    assert closed["ended_at"] is not None
    assert delete_response.status_code == 200
    assert delete_response.json()["data"]["session_id"] == session_to_delete["session_id"]
    assert deleted_lookup.status_code == 404


def test_session_lifecycle_routes_reject_non_member(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    viewer_user,
) -> None:
    project_id = _create_project(client, admin_auth_headers, "HTTP session denied")
    question_id = _create_question(client, admin_auth_headers, project_id)
    session = _create_operational_session(client, admin_auth_headers, project_id)

    denied_read = client.get(
        f"/sessions/by-link/{session['link_code']}",
        headers=viewer_user.headers,
    )
    assert denied_read.status_code == 404
    assert denied_read.json()["error"]["message"] == "Session does not exist."

    requests: list[Callable[[], Any]] = [
        lambda: client.post(
            f"/sessions/{session['session_id']}/promote",
            json={"primary_question_id": question_id},
            headers=viewer_user.headers,
        ),
        lambda: client.post(
            f"/sessions/{session['session_id']}/promote-to-dataset",
            json={"primary_question_id": question_id},
            headers=viewer_user.headers,
        ),
        lambda: client.patch(
            f"/sessions/{session['session_id']}",
            json={"status": "closed"},
            headers=viewer_user.headers,
        ),
        lambda: client.delete(
            f"/sessions/{session['session_id']}",
            headers=viewer_user.headers,
        ),
    ]

    for request in requests:
        response = request()
        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Project access required."
