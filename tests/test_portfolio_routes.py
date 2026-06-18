from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient

from lab_tracker.db_models import ProjectModel


class FakeDraftClient:
    model = "fake-gpt"

    def __init__(self, patch: dict[str, Any]) -> None:
        self.patch = patch

    def draft_from_note(self, **_: Any) -> dict[str, Any]:
        return self.patch

    def close(self) -> None:
        return None


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_user(
    client: TestClient,
    username: str,
    password: str = "secret",
    *,
    role: str = "viewer",
    headers: dict[str, str] | None = None,
) -> str:
    response = client.post(
        "/auth/register",
        json={"username": username, "password": password, "role": role},
        headers=headers or {},
    )
    assert response.status_code == 201
    return response.json()["data"]["access_token"]


def _create_project(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _create_question(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    *,
    text: str,
    status: str = "active",
) -> str:
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
    assert response.status_code == 201
    return response.json()["data"]["question_id"]


def _create_staged_dataset(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    question_id: str,
) -> str:
    response = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["dataset_id"]


def _commit_dataset(client: TestClient, headers: dict[str, str], dataset_id: str) -> None:
    upload = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("portfolio.bin", b"portfolio-bytes", "application/octet-stream")},
        headers=headers,
    )
    assert upload.status_code == 201
    response = client.patch(
        f"/datasets/{dataset_id}",
        json={"status": "committed"},
        headers=headers,
    )
    assert response.status_code == 200


def _graph_draft_patch(project_id: str) -> dict[str, Any]:
    return {
        "summary": "Drafted portfolio activity",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [
            {
                "client_ref": "portfolio-note",
                "op": "create",
                "entity_type": "note",
                "semantic_type": "create_note",
                "target_entity_id": None,
                "payload_json": json.dumps(
                    {
                        "project_id": project_id,
                        "raw_content": "Portfolio graph draft activity.",
                    }
                ),
                "rationale": "Capture a follow-up note from the source.",
                "confidence": 0.9,
                "source_refs": [],
            }
        ],
    }


def _parse_utc_timestamp(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def _portfolio_projects(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        project
        for project_group in payload["data"]
        for project in project_group["projects"]
    ]


def _triage_flags_by_key(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {flag["key"]: flag for flag in row["triage_flags"]}


def test_portfolio_summary_aggregates_project_health_rows(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Portfolio project")
    owner_token = _register_user(client, "portfolio-owner")
    owner_headers = _auth_headers(owner_token)
    owner_response = client.post(
        f"/projects/{project_id}/members",
        json={"username": "portfolio-owner", "role": "owner"},
        headers=admin_auth_headers,
    )
    assert owner_response.status_code == 201
    _register_user(client, "portfolio-viewer-member")
    _register_user(client, "portfolio-contributor-member")
    viewer_response = client.post(
        f"/projects/{project_id}/members",
        json={"username": "portfolio-viewer-member", "role": "viewer"},
        headers=admin_auth_headers,
    )
    contributor_response = client.post(
        f"/projects/{project_id}/members",
        json={"username": "portfolio-contributor-member", "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert viewer_response.status_code == 201
    assert contributor_response.status_code == 201

    active_question_id = _create_question(
        client,
        admin_auth_headers,
        project_id,
        text="Which signal stays active?",
    )
    _create_question(
        client,
        admin_auth_headers,
        project_id,
        text="Which staged question is still open?",
        status="staged",
    )
    committed_dataset_id = _create_staged_dataset(
        client,
        admin_auth_headers,
        project_id,
        active_question_id,
    )
    _commit_dataset(client, admin_auth_headers, committed_dataset_id)
    staged_dataset_id = _create_staged_dataset(
        client,
        admin_auth_headers,
        project_id,
        active_question_id,
    )

    analysis = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [staged_dataset_id],
            "method_hash": "portfolio-method",
            "code_version": "v1",
        },
        headers=admin_auth_headers,
    )
    assert analysis.status_code == 201
    claim = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "Portfolio claim awaiting review.",
            "confidence": 50.0,
        },
        headers=admin_auth_headers,
    )
    assert claim.status_code == 201
    note = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Recent portfolio activity."},
        headers=admin_auth_headers,
    )
    assert note.status_code == 201

    response = client.get("/portfolio/summary", headers=owner_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["total"] == 1
    assert payload["data"][0]["project_group"] is None
    assert payload["data"][0]["project_count"] == 1
    row = payload["data"][0]["projects"][0]
    assert row["project_id"] == project_id
    assert row["name"] == "Portfolio project"
    assert row["status"] == "active"
    assert row["open_question_count"] == 2
    assert row["draft_dataset_count"] == 1
    assert row["committed_dataset_count"] == 1
    assert row["staged_analysis_count"] == 1
    assert row["unreviewed_claim_count"] == 1
    assert row["last_activity_at"] is not None
    assert row["owners"] == [
        {
            "user_id": owner_response.json()["data"]["user_id"],
            "username": "portfolio-owner",
        }
    ]


def test_portfolio_summary_is_scoped_to_accessible_projects(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
):
    visible_question_id = _create_question(
        client,
        admin_auth_headers,
        scoped_project_member.visible_project_id,
        text="Visible project question?",
    )
    hidden_question_id = _create_question(
        client,
        admin_auth_headers,
        scoped_project_member.hidden_project_id,
        text="Hidden project question?",
    )
    _create_staged_dataset(
        client,
        admin_auth_headers,
        scoped_project_member.visible_project_id,
        visible_question_id,
    )
    _create_staged_dataset(
        client,
        admin_auth_headers,
        scoped_project_member.hidden_project_id,
        hidden_question_id,
    )

    scoped_response = client.get(
        "/portfolio/summary",
        headers=scoped_project_member.member_headers,
    )
    admin_response = client.get("/portfolio/summary", headers=admin_auth_headers)

    assert scoped_response.status_code == 200
    assert [item["project_id"] for item in _portfolio_projects(scoped_response.json())] == [
        scoped_project_member.visible_project_id
    ]
    assert scoped_response.json()["meta"]["total"] == 1

    assert admin_response.status_code == 200
    admin_project_ids = {
        item["project_id"] for item in _portfolio_projects(admin_response.json())
    }
    assert {
        scoped_project_member.visible_project_id,
        scoped_project_member.hidden_project_id,
    }.issubset(admin_project_ids)


def test_portfolio_summary_rejects_unauthenticated_and_hides_all_from_non_member(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    viewer_user,
):
    _create_project(client, admin_auth_headers, "Hidden from non-member portfolio")

    unauthenticated = client.get("/portfolio/summary")
    response = client.get("/portfolio/summary", headers=viewer_user.headers)

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    assert response.json()["data"] == []
    assert response.json()["meta"]["total"] == 0


def test_portfolio_summary_filters_project_status(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    active_project_id = _create_project(client, admin_auth_headers, "Active portfolio")
    archived_project_id = _create_project(client, admin_auth_headers, "Archived portfolio")
    archive_response = client.patch(
        f"/projects/{archived_project_id}",
        json={"status": "archived"},
        headers=admin_auth_headers,
    )
    assert archive_response.status_code == 200

    active_response = client.get(
        "/portfolio/summary?status=active",
        headers=admin_auth_headers,
    )
    archived_response = client.get(
        "/portfolio/summary?status=archived",
        headers=admin_auth_headers,
    )

    assert active_response.status_code == 200
    assert [item["project_id"] for item in _portfolio_projects(active_response.json())] == [
        active_project_id
    ]
    assert archived_response.status_code == 200
    assert [item["project_id"] for item in _portfolio_projects(archived_response.json())] == [
        archived_project_id
    ]


def test_portfolio_summary_paginates_group_buckets_and_validates_bounds(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    group_response = client.post(
        "/groups",
        json={"name": "Paged portfolio group"},
        headers=admin_auth_headers,
    )
    assert group_response.status_code == 201
    grouped_project_id = _create_project(client, admin_auth_headers, "Paged grouped")
    ungrouped_project_id = _create_project(client, admin_auth_headers, "Paged ungrouped")
    assign_response = client.patch(
        f"/projects/{grouped_project_id}",
        json={"group_id": group_response.json()["data"]["group_id"]},
        headers=admin_auth_headers,
    )
    assert assign_response.status_code == 200

    first_page = client.get(
        "/portfolio/summary?limit=1",
        headers=admin_auth_headers,
    )
    second_page = client.get(
        "/portfolio/summary?limit=1&offset=1",
        headers=admin_auth_headers,
    )

    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert first_page.json()["meta"]["total"] == 2
    assert second_page.json()["meta"]["total"] == 2
    page_project_ids = {
        item["project_id"]
        for payload in (first_page.json(), second_page.json())
        for item in _portfolio_projects(payload)
    }
    assert page_project_ids == {grouped_project_id, ungrouped_project_id}

    for query in ("limit=0", "limit=201", "offset=-1"):
        invalid = client.get(f"/portfolio/summary?{query}", headers=admin_auth_headers)
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "validation_error"


def test_portfolio_summary_groups_projects_by_project_group(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    owner_headers = _auth_headers(
        _register_user(
            client,
            "portfolio-group-owner",
            role="editor",
            headers=admin_auth_headers,
        )
    )
    group_response = client.post(
        "/groups",
        json={"name": "Olfaction lab"},
        headers=owner_headers,
    )
    assert group_response.status_code == 201
    group = group_response.json()["data"]
    grouped_project_ids = [
        _create_project(client, admin_auth_headers, "Grouped portfolio A"),
        _create_project(client, admin_auth_headers, "Grouped portfolio B"),
    ]
    ungrouped_project_id = _create_project(
        client,
        admin_auth_headers,
        "Ungrouped portfolio",
    )
    for project_id in grouped_project_ids:
        assign_response = client.patch(
            f"/projects/{project_id}",
            json={"group_id": group["group_id"]},
            headers=admin_auth_headers,
        )
        assert assign_response.status_code == 200

    response = client.get("/portfolio/summary", headers=owner_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["total"] == 1
    assert len(payload["data"]) == 1
    bucket = payload["data"][0]
    assert bucket["project_group"]["group_id"] == group["group_id"]
    assert bucket["project_group"]["name"] == "Olfaction lab"
    assert bucket["project_count"] == 2
    assert {item["project_id"] for item in bucket["projects"]} == set(grouped_project_ids)
    assert ungrouped_project_id not in {
        item["project_id"] for item in _portfolio_projects(payload)
    }

    page_response = client.get("/portfolio/summary?limit=1", headers=owner_headers)
    assert page_response.status_code == 200
    page_payload = page_response.json()
    assert page_payload["meta"]["total"] == 1
    assert len(page_payload["data"]) == 1
    assert page_payload["data"][0]["project_count"] == 2
    assert {item["project_id"] for item in page_payload["data"][0]["projects"]} == set(
        grouped_project_ids
    )


def test_portfolio_summary_normalizes_graph_change_set_activity(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Graph draft portfolio")
    note = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Source note for graph draft."},
        headers=admin_auth_headers,
    )
    assert note.status_code == 201
    note_id = note.json()["data"]["note_id"]
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _graph_draft_patch(project_id)
    )
    draft_response = client.post(
        f"/notes/{note_id}/graph-drafts",
        headers=admin_auth_headers,
    )
    assert draft_response.status_code == 201

    response = client.get("/portfolio/summary", headers=admin_auth_headers)

    assert response.status_code == 200
    rows = _portfolio_projects(response.json())
    row = next(item for item in rows if item["project_id"] == project_id)
    assert _parse_utc_timestamp(row["last_activity_at"]) == _parse_utc_timestamp(
        draft_response.json()["data"]["updated_at"]
    )


def test_portfolio_summary_uses_project_timestamp_for_childless_project(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Childless portfolio")
    created_at = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    updated_at = created_at + timedelta(hours=3)
    with client.app.state.db_session_factory() as session:
        project = session.get(ProjectModel, project_id)
        project.created_at = created_at
        project.updated_at = updated_at
        session.commit()

    response = client.get("/portfolio/summary", headers=admin_auth_headers)

    assert response.status_code == 200
    rows = _portfolio_projects(response.json())
    row = next(item for item in rows if item["project_id"] == project_id)
    assert _parse_utc_timestamp(row["last_activity_at"]) == updated_at


def test_portfolio_summary_derives_lab_health_triage_flags(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    stale_project_id = _create_project(client, admin_auth_headers, "Stale portfolio")
    old_timestamp = datetime.now(timezone.utc) - timedelta(days=45)
    with client.app.state.db_session_factory() as session:
        stale_project = session.get(ProjectModel, stale_project_id)
        stale_project.created_at = old_timestamp
        stale_project.updated_at = old_timestamp
        session.commit()

    project_id = _create_project(client, admin_auth_headers, "Flagged portfolio")
    question_id = _create_question(
        client,
        admin_auth_headers,
        project_id,
        text="Which analysis gaps remain?",
    )
    analyzed_dataset_id = _create_staged_dataset(
        client,
        admin_auth_headers,
        project_id,
        question_id,
    )
    _commit_dataset(client, admin_auth_headers, analyzed_dataset_id)
    unanalyzed_dataset_id = _create_staged_dataset(
        client,
        admin_auth_headers,
        project_id,
        question_id,
    )
    _commit_dataset(client, admin_auth_headers, unanalyzed_dataset_id)
    analysis = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [analyzed_dataset_id],
            "method_hash": "triage-method",
            "code_version": "v1",
        },
        headers=admin_auth_headers,
    )
    assert analysis.status_code == 201
    committed_analysis = client.post(
        f"/analyses/{analysis.json()['data']['analysis_id']}/commit",
        json={},
        headers=admin_auth_headers,
    )
    assert committed_analysis.status_code == 200
    claim = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "This claim still needs review.",
            "confidence": 55.0,
        },
        headers=admin_auth_headers,
    )
    assert claim.status_code == 201
    overdue_goal = client.post(
        f"/projects/{project_id}/goals",
        json={
            "goal_type": "other",
            "title": "Overdue portfolio goal",
            "target_date": (date.today() - timedelta(days=1)).isoformat(),
        },
        headers=admin_auth_headers,
    )
    assert overdue_goal.status_code == 201

    response = client.get("/portfolio/summary", headers=admin_auth_headers)

    assert response.status_code == 200
    rows = {item["project_id"]: item for item in _portfolio_projects(response.json())}
    stale_flags = _triage_flags_by_key(rows[stale_project_id])
    assert stale_flags["stale_project"] == {
        "count": 1,
        "key": "stale_project",
        "label": "No activity in 30 days",
        "severity": "warning",
    }
    flags = _triage_flags_by_key(rows[project_id])
    assert flags["unanswered_questions"]["count"] == 1
    assert flags["datasets_without_analyses"]["count"] == 1
    assert flags["analyses_without_claims"]["count"] == 1
    assert flags["unreviewed_claims"] == {
        "count": 1,
        "key": "unreviewed_claims",
        "label": "Unreviewed claims",
        "severity": "info",
    }
    assert flags["overdue_goals"] == {
        "count": 1,
        "key": "overdue_goals",
        "label": "Overdue goals",
        "severity": "critical",
    }
