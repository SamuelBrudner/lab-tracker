from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from lab_tracker.auth import Role
from lab_tracker.db_models import RecordExportEventModel


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_user(
    client: TestClient,
    *,
    role: Role,
) -> tuple[dict[str, str], str]:
    username = f"record-export-{role.value}-{uuid4().hex[:8]}"
    user = client.app.state.auth_service.register_user(
        username=username,
        password="secret",
        role=role,
    )
    login_response = client.post(
        "/auth/login",
        json={"username": username, "password": "secret"},
    )
    assert login_response.status_code == 200
    return _auth_headers(login_response.json()["data"]["access_token"]), str(user.user_id)


def _create_project_bundle(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_name: str,
    group_id: str | None = None,
) -> dict[str, str]:
    project_payload = {"name": project_name}
    if group_id is not None:
        project_payload["group_id"] = group_id
    project_id = client.post(
        "/projects",
        json=project_payload,
        headers=headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": f"{project_name} question",
            "question_type": "descriptive",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    dataset_id = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    ).json()["data"]["dataset_id"]
    note_id = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": f"{project_name} note"},
        headers=headers,
    ).json()["data"]["note_id"]
    analysis_id = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": f"{project_name}-method",
            "code_version": f"{project_name}-code",
        },
        headers=headers,
    ).json()["data"]["analysis_id"]
    claim_id = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": f"{project_name} claim",
            "confidence": 75,
            "supported_by_dataset_ids": [dataset_id],
            "supported_by_analysis_ids": [analysis_id],
        },
        headers=headers,
    ).json()["data"]["claim_id"]
    return {
        "project_id": project_id,
        "question_id": question_id,
        "dataset_id": dataset_id,
        "note_id": note_id,
        "analysis_id": analysis_id,
        "claim_id": claim_id,
    }


def _graph_ids(export_payload: dict[str, object]) -> set[str]:
    graph = export_payload["provenance"]["@graph"]
    assert isinstance(graph, list)
    return {str(node["@id"]) for node in graph if isinstance(node, dict) and "@id" in node}


def _record_export_event_count(client: TestClient) -> int:
    with client.app.state.db_session_factory() as session:
        return len(list(session.scalars(select(RecordExportEventModel.export_id))))


def test_record_export_returns_scoped_dump_and_provenance_for_user_and_group(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    source_headers, source_user_id = _register_user(client, role=Role.ADMIN)
    group_owner_headers, group_owner_id = _register_user(client, role=Role.VIEWER)
    viewer_headers, _ = _register_user(client, role=Role.VIEWER)

    group_response = client.post(
        "/groups",
        json={"name": "Export lab group"},
        headers=admin_auth_headers,
    )
    assert group_response.status_code == 201
    group_id = group_response.json()["data"]["group_id"]
    owner_response = client.post(
        f"/groups/{group_id}/members",
        json={"user_id": group_owner_id, "role": "owner"},
        headers=admin_auth_headers,
    )
    assert owner_response.status_code == 201

    group_records = _create_project_bundle(
        client,
        source_headers,
        project_name="Grouped export",
        group_id=group_id,
    )
    outside_records = _create_project_bundle(
        client,
        source_headers,
        project_name="Outside export",
    )

    global_denied = client.post(
        f"/record-exports/users/{source_user_id}",
        headers=group_owner_headers,
    )
    group_denied = client.post(
        f"/groups/{group_id}/record-exports/users/{source_user_id}",
        headers=viewer_headers,
    )
    assert global_denied.status_code == 401
    assert group_denied.status_code == 401

    unsafe_read = client.get(
        f"/record-exports/users/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert unsafe_read.status_code == 405
    assert _record_export_event_count(client) == 0

    global_export = client.post(
        f"/record-exports/users/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert global_export.status_code == 200, global_export.text
    global_payload = global_export.json()["data"]
    assert global_payload["user_id"] == source_user_id
    assert global_payload["group_id"] is None
    assert {item["question_id"] for item in global_payload["records"]["questions"]} == {
        group_records["question_id"],
        outside_records["question_id"],
    }
    assert {item["dataset_id"] for item in global_payload["records"]["datasets"]} == {
        group_records["dataset_id"],
        outside_records["dataset_id"],
    }
    assert {item["analysis_id"] for item in global_payload["records"]["analyses"]} == {
        group_records["analysis_id"],
        outside_records["analysis_id"],
    }
    assert {item["claim_id"] for item in global_payload["records"]["claims"]} == {
        group_records["claim_id"],
        outside_records["claim_id"],
    }
    assert {item["note_id"] for item in global_payload["records"]["notes"]} == {
        group_records["note_id"],
        outside_records["note_id"],
    }

    graph_ids = _graph_ids(global_payload)
    assert f"http://testserver/agents/{source_user_id}" in graph_ids
    assert f"http://testserver/datasets/{group_records['dataset_id']}" in graph_ids
    assert f"http://testserver/analyses/{outside_records['analysis_id']}" in graph_ids
    assert f"http://testserver/claims/{group_records['claim_id']}" in graph_ids
    assert f"http://testserver/notes/{outside_records['note_id']}" in graph_ids

    group_export = client.post(
        f"/groups/{group_id}/record-exports/users/{source_user_id}",
        headers=group_owner_headers,
    )
    assert group_export.status_code == 200, group_export.text
    group_payload = group_export.json()["data"]
    assert group_payload["user_id"] == source_user_id
    assert group_payload["group_id"] == group_id
    assert group_payload["project_ids"] == [group_records["project_id"]]
    assert [item["question_id"] for item in group_payload["records"]["questions"]] == [
        group_records["question_id"]
    ]
    assert [item["dataset_id"] for item in group_payload["records"]["datasets"]] == [
        group_records["dataset_id"]
    ]
    assert [item["analysis_id"] for item in group_payload["records"]["analyses"]] == [
        group_records["analysis_id"]
    ]
    assert [item["claim_id"] for item in group_payload["records"]["claims"]] == [
        group_records["claim_id"]
    ]
    assert [item["note_id"] for item in group_payload["records"]["notes"]] == [
        group_records["note_id"]
    ]

    group_graph_ids = _graph_ids(group_payload)
    assert f"http://testserver/datasets/{group_records['dataset_id']}" in group_graph_ids
    assert f"http://testserver/datasets/{outside_records['dataset_id']}" not in group_graph_ids


def test_record_export_returns_not_found_for_missing_user(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    response = client.post(
        f"/record-exports/users/{uuid4()}",
        headers=admin_auth_headers,
    )

    assert response.status_code == 404
