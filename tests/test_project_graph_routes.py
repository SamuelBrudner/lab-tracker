from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from lab_tracker.auth import Role


def _ids(items: list[dict[str, object]]) -> set[str]:
    return {str(item["id"]) for item in items}


def _edge_ids(items: list[dict[str, object]]) -> set[str]:
    return {str(item["id"]) for item in items}


def _auth_headers(client: TestClient, *, role: Role = Role.VIEWER) -> dict[str, str]:
    username = f"graph-user-{uuid4().hex[:8]}"
    password = "secret"
    client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=role,
    )
    login = client.post("/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['data']['access_token']}"}


def _create_graph_fixture(
    client: TestClient,
    headers: dict[str, str],
) -> dict[str, str]:
    project_id = client.post(
        "/projects",
        json={"name": "Project graph"},
        headers=headers,
    ).json()["data"]["project_id"]
    root_question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": 'Can "escape" survive?\nYes',
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    child_question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "What evidence is linked?",
            "question_type": "hypothesis_driven",
            "status": "active",
            "parent_question_ids": [root_question_id],
        },
        headers=headers,
    ).json()["data"]["question_id"]
    refactor = client.post(
        f"/questions/{root_question_id}/refactor",
        json={
            "replacement": {
                "text": "Can escaped labels be rendered?",
                "question_type": "descriptive",
                "status": "active",
            },
            "child_question_ids_to_reparent": [child_question_id],
            "reason": "Tighter wording.",
        },
        headers=headers,
    ).json()["data"]
    replacement_question_id = refactor["replacement_question"]["question_id"]
    scientific_session_id = client.post(
        "/sessions",
        json={
            "project_id": project_id,
            "session_type": "scientific",
            "primary_question_id": child_question_id,
        },
        headers=headers,
    ).json()["data"]["session_id"]
    source_session_id = client.post(
        "/sessions",
        json={
            "project_id": project_id,
            "session_type": "operational",
        },
        headers=headers,
    ).json()["data"]["session_id"]
    note_id = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Figure note",
            "targets": [{"entity_type": "question", "entity_id": child_question_id}],
        },
        headers=headers,
    ).json()["data"]["note_id"]
    dataset_id = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": child_question_id,
            "secondary_question_ids": [replacement_question_id],
            "status": "committed",
                "commit_manifest": {
                    "files": [{"path": "raw/data.csv", "checksum": "abc"}],
                    "source_session_id": source_session_id,
                },
            },
            headers=headers,
        ).json()["data"]["dataset_id"]
    analysis_id = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "method-1",
            "code_version": "code-1",
            "status": "committed",
        },
        headers=headers,
    ).json()["data"]["analysis_id"]
    claim_id = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "Evidence supports the claim.",
            "confidence": 77,
            "status": "supported",
            "supported_by_dataset_ids": [dataset_id],
            "supported_by_analysis_ids": [analysis_id],
            "answers_question_ids": [child_question_id],
        },
        headers=headers,
    ).json()["data"]["claim_id"]
    viz_id = client.post(
        "/visualizations",
        json={
            "analysis_id": analysis_id,
            "viz_type": "line plot",
            "file_path": "figures/plot.png",
            "caption": "Main figure",
            "related_claim_ids": [claim_id],
        },
        headers=headers,
    ).json()["data"]["viz_id"]
    return {
        "analysis_id": analysis_id,
        "child_question_id": child_question_id,
        "claim_id": claim_id,
        "dataset_id": dataset_id,
        "note_id": note_id,
        "project_id": project_id,
        "replacement_question_id": replacement_question_id,
        "root_question_id": root_question_id,
        "scientific_session_id": scientific_session_id,
        "source_session_id": source_session_id,
        "viz_id": viz_id,
    }


def test_project_graph_questions_view_contains_question_edges(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    ids = _create_graph_fixture(client, admin_auth_headers)

    response = client.get(
        f"/projects/{ids['project_id']}/graph?view=questions",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["project_id"] == ids["project_id"]
    assert payload["view"] == "questions"
    assert _ids(payload["nodes"]) == {
        f"question:{ids['child_question_id']}",
        f"question:{ids['replacement_question_id']}",
        f"question:{ids['root_question_id']}",
    }
    assert _edge_ids(payload["edges"]) == {
        (
            "question_parent:"
            f"question:{ids['replacement_question_id']}->question:{ids['child_question_id']}"
        ),
        (
            "question_superseded_by:"
            f"question:{ids['root_question_id']}->question:{ids['replacement_question_id']}"
        ),
        (
            "question_supersedes:"
            f"question:{ids['replacement_question_id']}->question:{ids['root_question_id']}"
        ),
    }


def test_project_graph_evidence_and_full_views_include_expected_links(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    ids = _create_graph_fixture(client, admin_auth_headers)

    evidence = client.get(
        f"/projects/{ids['project_id']}/graph",
        headers=admin_auth_headers,
    ).json()["data"]
    full = client.get(
        f"/projects/{ids['project_id']}/graph?view=full",
        headers=admin_auth_headers,
    ).json()["data"]

    assert evidence["view"] == "evidence"
    assert f"note:{ids['note_id']}" not in _ids(evidence["nodes"])
    assert f"session:{ids['scientific_session_id']}" not in _ids(evidence["nodes"])
    assert f"session:{ids['source_session_id']}" not in _ids(evidence["nodes"])
    assert {
        (
            "dataset_question_primary:"
            f"question:{ids['child_question_id']}->dataset:{ids['dataset_id']}"
        ),
        (
            "dataset_question_secondary:"
            f"question:{ids['replacement_question_id']}->dataset:{ids['dataset_id']}"
        ),
        f"analysis_dataset:dataset:{ids['dataset_id']}->analysis:{ids['analysis_id']}",
        f"claim_dataset_support:dataset:{ids['dataset_id']}->claim:{ids['claim_id']}",
        f"claim_analysis_support:analysis:{ids['analysis_id']}->claim:{ids['claim_id']}",
        f"claim_question_answers:claim:{ids['claim_id']}->question:{ids['child_question_id']}",
        f"visualization_analysis:analysis:{ids['analysis_id']}->visualization:{ids['viz_id']}",
        f"visualization_claim:claim:{ids['claim_id']}->visualization:{ids['viz_id']}",
    }.issubset(_edge_ids(evidence["edges"]))

    assert f"note:{ids['note_id']}" in _ids(full["nodes"])
    assert f"session:{ids['scientific_session_id']}" in _ids(full["nodes"])
    assert f"session:{ids['source_session_id']}" in _ids(full["nodes"])
    assert {
        f"note_target_question:note:{ids['note_id']}->question:{ids['child_question_id']}",
        (
            "session_question:"
            f"question:{ids['child_question_id']}->session:{ids['scientific_session_id']}"
        ),
        (
            "dataset_source_session:"
            f"session:{ids['source_session_id']}->dataset:{ids['dataset_id']}"
        ),
    }.issubset(_edge_ids(full["edges"]))


def test_project_graph_mermaid_export_is_stable_and_escaped(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    ids = _create_graph_fixture(client, admin_auth_headers)

    response = client.get(
        f"/projects/{ids['project_id']}/graph/mermaid?view=questions",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/vnd.mermaid")
    first = response.text
    second = client.get(
        f"/projects/{ids['project_id']}/graph/mermaid?view=questions",
        headers=admin_auth_headers,
    ).text
    assert first == second
    assert first.startswith("graph LR\n")
    assert 'Can \\"escape\\" survive? Yes' in first
    assert "\nYes" not in first


def test_project_graph_routes_require_project_access(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    ids = _create_graph_fixture(client, admin_auth_headers)
    viewer_headers = _auth_headers(client, role=Role.VIEWER)

    graph_response = client.get(
        f"/projects/{ids['project_id']}/graph",
        headers=viewer_headers,
    )
    mermaid_response = client.get(
        f"/projects/{ids['project_id']}/graph/mermaid",
        headers=viewer_headers,
    )
    unauthenticated_response = client.get(f"/projects/{ids['project_id']}/graph")

    assert graph_response.status_code == 401
    assert mermaid_response.status_code == 401
    assert unauthenticated_response.status_code == 401
