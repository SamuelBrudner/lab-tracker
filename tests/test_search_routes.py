from __future__ import annotations

from fastapi.testclient import TestClient


def test_search_endpoint_returns_questions_and_notes(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_response = client.post(
        "/projects",
        json={"name": "Search API Project", "description": ""},
        headers=admin_auth_headers,
    )
    assert project_response.status_code == 201
    project_id = project_response.json()["data"]["project_id"]

    question_response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "What is the baseline distribution?",
            "question_type": "descriptive",
            "hypothesis": "Baseline differs by condition",
        },
        headers=admin_auth_headers,
    )
    assert question_response.status_code == 201
    question_id = question_response.json()["data"]["question_id"]

    note_response = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Meeting notes: baseline controls and QC",
        },
        headers=admin_auth_headers,
    )
    assert note_response.status_code == 201
    note_id = note_response.json()["data"]["note_id"]

    search_response = client.get(
        "/search",
        params={"q": "baseline", "project_id": project_id},
        headers=admin_auth_headers,
    )
    assert search_response.status_code == 200
    payload = search_response.json()["data"]
    assert {item["question_id"] for item in payload["questions"]} == {question_id}
    assert {item["note_id"] for item in payload["notes"]} == {note_id}

