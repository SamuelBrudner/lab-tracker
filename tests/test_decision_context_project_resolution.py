"""Decision context resolves projects by id, not through a list window (M24)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import lab_tracker.decision_context_use_case as decision_context_use_case


def _create_project(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post("/projects", json={"name": name, "description": ""}, headers=headers)
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["project_id"])


def _create_question(
    client: TestClient, headers: dict[str, str], project_id: str, text: str
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
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["question_id"])


@pytest.fixture()
def one_project_window(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a lab with more projects than the lookup window: only the oldest
    # project would fit in a list_projects(limit=CONTEXT_LOOKUP_LIMIT) page.
    monkeypatch.setattr(decision_context_use_case, "CONTEXT_LOOKUP_LIMIT", 1)


@pytest.mark.usefixtures("one_project_window")
def test_anchor_only_call_resolves_project_outside_lookup_window(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    _create_project(client, admin_auth_headers, "Older project")
    newer_project_id = _create_project(client, admin_auth_headers, "Newer project")
    question_id = _create_question(
        client, admin_auth_headers, newer_project_id, "Which lookup window hides me?"
    )

    response = client.post(
        "/assistant/decision-context",
        json={"task_kind": "summary", "query": "lookup window", "question_id": question_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert "error" not in payload, payload
    assert payload["data"]["scope"]["project"]["project_id"] == newer_project_id


def test_search_resolves_single_match_outside_lookup_window(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The two older projects fill a two-project window; the match is newer.
    monkeypatch.setattr(decision_context_use_case, "CONTEXT_LOOKUP_LIMIT", 2)
    _create_project(client, admin_auth_headers, "Oldest project")
    _create_project(client, admin_auth_headers, "Older project")
    newer_project_id = _create_project(client, admin_auth_headers, "Newer project")
    _create_question(client, admin_auth_headers, newer_project_id, "Does quokka odor matter?")

    response = client.post(
        "/assistant/decision-context",
        json={"task_kind": "summary", "query": "quokka"},
        headers=admin_auth_headers,
    )

    payload = response.json()
    assert "error" not in payload, payload
    assert payload["data"]["scope"]["project"]["project_id"] == newer_project_id


def test_ambiguous_search_lists_matches_outside_lookup_window(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A two-project window holds the older project and the second match only.
    monkeypatch.setattr(decision_context_use_case, "CONTEXT_LOOKUP_LIMIT", 2)
    _create_project(client, admin_auth_headers, "Older project")
    second_id = _create_project(client, admin_auth_headers, "Second project")
    third_id = _create_project(client, admin_auth_headers, "Third project")
    _create_question(client, admin_auth_headers, second_id, "Does wombat odor matter?")
    _create_question(client, admin_auth_headers, third_id, "Is wombat odor stable?")

    response = client.post(
        "/assistant/decision-context",
        json={"task_kind": "summary", "query": "wombat"},
        headers=admin_auth_headers,
    )

    error = response.json()["error"]
    assert error["code"] == "ambiguous_project"
    assert [item["project_id"] for item in error["candidate_projects"]] == sorted(
        [second_id, third_id]
    )
    assert {item["reason"] for item in error["candidate_projects"]} == {"search_match"}


def test_search_match_lookup_cut_at_the_limit_is_never_treated_as_unique(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(decision_context_use_case, "CONTEXT_LOOKUP_LIMIT", 1)
    first_id = _create_project(client, admin_auth_headers, "First project")
    second_id = _create_project(client, admin_auth_headers, "Second project")
    _create_question(client, admin_auth_headers, first_id, "Does numbat odor matter?")
    _create_question(client, admin_auth_headers, second_id, "Is numbat odor stable?")

    response = client.post(
        "/assistant/decision-context",
        json={"task_kind": "summary", "query": "numbat"},
        headers=admin_auth_headers,
    )

    error = response.json()["error"]
    assert error["code"] == "ambiguous_project"
    assert error["candidate_projects_truncated"] is True


def test_ambiguous_fallback_reports_active_candidate_truncation(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    for name in ("Project A", "Project B", "Project C"):
        _create_project(client, admin_auth_headers, name)

    response = client.post(
        "/assistant/decision-context",
        json={"task_kind": "summary", "query": "no-such-term-anywhere", "limit": 2},
        headers=admin_auth_headers,
    )

    error = response.json()["error"]
    assert error["code"] == "ambiguous_project"
    assert len(error["candidate_projects"]) == 2
    assert error["candidate_projects_total"] == 3
    assert error["candidate_projects_truncated"] is True
