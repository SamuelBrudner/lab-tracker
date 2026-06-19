from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from lab_tracker.auth import Role
from lab_tracker.db_models import QuestionModel


def _login_headers(client: TestClient, username: str, password: str = "secret") -> dict[str, str]:
    response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def _register_user(
    client: TestClient,
    *,
    role: Role = Role.ADMIN,
) -> tuple[dict[str, str], str]:
    username = f"offboarding-{role.value}-{uuid4().hex[:8]}"
    user = client.app.state.auth_service.register_user(
        username=username,
        password="secret",
        role=role,
    )
    return _login_headers(client, username), str(user.user_id)


def _create_attributed_question(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    label: str,
) -> str:
    response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": f"{label} question",
            "question_type": "descriptive",
        },
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["question_id"]


def _clear_question_attribution_fk(client: TestClient, question_id: str) -> None:
    with client.app.state.db_session_factory() as session:
        row = session.get(QuestionModel, question_id)
        assert row is not None
        row.created_by_user_id = None
        session.commit()


def _create_session_and_visualization_only_for_user(
    client: TestClient,
    *,
    admin_headers: dict[str, str],
    source_headers: dict[str, str],
    project_id: str,
) -> tuple[str, str]:
    question_response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Admin scaffold question",
            "question_type": "descriptive",
        },
        headers=admin_headers,
    )
    assert question_response.status_code == 201, question_response.text
    question_id = question_response.json()["data"]["question_id"]
    dataset_response = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=admin_headers,
    )
    assert dataset_response.status_code == 201, dataset_response.text
    dataset_id = dataset_response.json()["data"]["dataset_id"]
    analysis_response = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "admin-method",
            "code_version": "admin-code",
        },
        headers=admin_headers,
    )
    assert analysis_response.status_code == 201, analysis_response.text
    analysis_id = analysis_response.json()["data"]["analysis_id"]
    session_response = client.post(
        "/sessions",
        json={"project_id": project_id, "session_type": "operational"},
        headers=source_headers,
    )
    assert session_response.status_code == 201, session_response.text
    visualization_response = client.post(
        "/visualizations",
        json={
            "analysis_id": analysis_id,
            "viz_type": "line",
            "file_path": "figures/source-only.png",
        },
        headers=source_headers,
    )
    assert visualization_response.status_code == 201, visualization_response.text
    return (
        session_response.json()["data"]["session_id"],
        visualization_response.json()["data"]["viz_id"],
    )


def test_project_membership_revoke_requires_export_for_attributed_records(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    source_headers, source_user_id = _register_user(client)
    project_id = client.post(
        "/projects",
        json={"name": "Direct offboarding"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    member = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": source_user_id, "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert member.status_code == 201
    _create_attributed_question(
        client,
        source_headers,
        project_id=project_id,
        label="handoff",
    )

    blocked = client.delete(
        f"/projects/{project_id}/members/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert blocked.status_code == 422
    assert "Export or reassign" in blocked.json()["error"]["message"]

    export = client.post(
        f"/record-exports/users/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert export.status_code == 200
    assert export.json()["data"]["export_event_id"]

    removed = client.delete(
        f"/projects/{project_id}/members/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert removed.status_code == 200


def test_project_membership_revoke_requires_export_for_session_and_visualization(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    source_headers, source_user_id = _register_user(client, role=Role.VIEWER)
    project_id = client.post(
        "/projects",
        json={"name": "Session visualization offboarding"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    member = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": source_user_id, "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert member.status_code == 201
    session_id, viz_id = _create_session_and_visualization_only_for_user(
        client,
        admin_headers=admin_auth_headers,
        source_headers=source_headers,
        project_id=project_id,
    )

    blocked = client.delete(
        f"/projects/{project_id}/members/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert blocked.status_code == 422
    assert "Export or reassign" in blocked.json()["error"]["message"]

    export = client.post(
        f"/record-exports/users/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert export.status_code == 200, export.text
    exported_records = export.json()["data"]["records"]
    assert [item["session_id"] for item in exported_records["sessions"]] == [session_id]
    assert [item["viz_id"] for item in exported_records["visualizations"]] == [viz_id]

    removed = client.delete(
        f"/projects/{project_id}/members/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert removed.status_code == 200


def test_project_membership_revoke_blocks_legacy_null_fk_records(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    source_headers, source_user_id = _register_user(client)
    project_id = client.post(
        "/projects",
        json={"name": "Legacy offboarding"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    member = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": source_user_id, "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert member.status_code == 201
    question_id = _create_attributed_question(
        client,
        source_headers,
        project_id=project_id,
        label="legacy handoff",
    )
    _clear_question_attribution_fk(client, question_id)

    blocked = client.delete(
        f"/projects/{project_id}/members/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert blocked.status_code == 422
    assert "Export or reassign" in blocked.json()["error"]["message"]

    export = client.post(
        f"/record-exports/users/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert export.status_code == 200
    exported_questions = export.json()["data"]["records"]["questions"]
    assert [item["question_id"] for item in exported_questions] == [question_id]

    removed = client.delete(
        f"/projects/{project_id}/members/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert removed.status_code == 200


def test_group_bulk_offboard_allows_reassignment_before_revoke(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    source_headers, source_user_id = _register_user(client)
    _, successor_user_id = _register_user(client, role=Role.VIEWER)
    group_id = client.post(
        "/groups",
        json={"name": "Bulk offboarding"},
        headers=admin_auth_headers,
    ).json()["data"]["group_id"]
    project_id = client.post(
        "/projects",
        json={"name": "Bulk project", "group_id": group_id},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    onboard = client.post(
        f"/groups/{group_id}/project-memberships",
        json={"user_id": source_user_id, "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert onboard.status_code == 200
    _create_attributed_question(
        client,
        source_headers,
        project_id=project_id,
        label="bulk",
    )

    blocked = client.delete(
        f"/groups/{group_id}/project-memberships/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert blocked.status_code == 422

    reassignment = client.post(
        "/ownership-reassignments",
        json={
            "from_user_id": source_user_id,
            "to_user_id": successor_user_id,
            "reason": "Offboarding",
        },
        headers=admin_auth_headers,
    )
    assert reassignment.status_code == 201

    removed = client.delete(
        f"/groups/{group_id}/project-memberships/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert removed.status_code == 200
    assert [item["user_id"] for item in removed.json()["data"]] == [source_user_id]
