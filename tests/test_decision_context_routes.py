from __future__ import annotations

from fastapi.testclient import TestClient


def test_decision_context_route_returns_research_writing_graph_slice(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = client.post(
        "/projects",
        json={"name": "Decision Context Project", "description": ""},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Which baseline controls matter?",
            "question_type": "descriptive",
            "status": "active",
            "hypothesis": "Baseline controls change the interpretation.",
        },
        headers=admin_auth_headers,
    ).json()["data"]["question_id"]
    client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Baseline control note",
            "targets": [{"entity_type": "question", "entity_id": question_id}],
            "status": "committed",
        },
        headers=admin_auth_headers,
    )
    dataset_id = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "status": "committed",
            "commit_manifest": {
                "files": [{"path": "data.csv", "checksum": "abc123"}],
            },
        },
        headers=admin_auth_headers,
    ).json()["data"]["dataset_id"]
    analysis_id = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "method-1",
            "code_version": "v1",
            "status": "committed",
        },
        headers=admin_auth_headers,
    ).json()["data"]["analysis_id"]
    claim_id = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "Baseline controls change behavior.",
            "confidence": 0.8,
            "status": "supported",
            "supported_by_dataset_ids": [dataset_id],
            "supported_by_analysis_ids": [analysis_id],
        },
        headers=admin_auth_headers,
    ).json()["data"]["claim_id"]
    visualization_id = client.post(
        "/visualizations",
        json={
            "analysis_id": analysis_id,
            "viz_type": "line",
            "file_path": "figures/baseline.png",
            "caption": "Baseline comparison",
            "related_claim_ids": [claim_id],
        },
        headers=admin_auth_headers,
    ).json()["data"]["viz_id"]

    response = client.post(
        "/assistant/decision-context",
        json={
            "task_kind": "research_writing",
            "query": "baseline controls",
            "project_id": project_id,
            "limit": 5,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    data = payload["data"]
    assert data["task_kind"] == "research_writing"
    assert data["scope"]["project"]["project_id"] == project_id
    assert data["questions"][0]["question_id"] == question_id
    assert "search_match" in data["questions"][0]["relevance_reasons"]
    assert data["claims"][0]["claim_id"] == claim_id
    assert data["visualizations"][0]["viz_id"] == visualization_id
    assert data["task_guidance"]["candidate_outputs"][0]["entity_type"] == "claim"
    assert data["write_front_door"]["resolved_scope"]["project_id"] == project_id
    assert data["write_front_door"]["candidate_ids"]["claims"][0]["entity_id"] == claim_id
    assert data["write_front_door"]["candidate_ids"]["datasets"][0]["entity_id"] == (
        dataset_id
    )
    assert {
        item["entity"]["entity_type"] for item in data["evidence_map"]
    } == {"dataset", "analysis", "claim", "visualization"}


def test_decision_context_route_returns_structured_ambiguity(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    for name in ["Project A", "Project B"]:
        client.post(
            "/projects",
            json={"name": name, "description": ""},
            headers=admin_auth_headers,
        )

    response = client.post(
        "/assistant/decision-context",
        json={"task_kind": "plot", "query": "baseline"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["code"] == "ambiguous_project"
    assert len(payload["error"]["candidate_projects"]) >= 2


def test_decision_context_route_rejects_invalid_task_kind(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    response = client.post(
        "/assistant/decision-context",
        json={"task_kind": "figure", "query": "baseline"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["code"] == "invalid_task_kind"
    assert "research_writing" in payload["error"]["allowed_task_kinds"]


def test_decision_context_route_reports_truncation_metadata(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = client.post(
        "/projects",
        json={"name": "Truncated Context Project", "description": ""},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    for index in range(2):
        client.post(
            "/questions",
            json={
                "project_id": project_id,
                "text": f"Which control replicate {index} matters?",
                "question_type": "descriptive",
                "status": "active",
            },
            headers=admin_auth_headers,
        )

    response = client.post(
        "/assistant/decision-context",
        json={
            "task_kind": "analysis",
            "query": "control replicate",
            "project_id": project_id,
            "limit": 1,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    truncation = response.json()["data"]["truncation"]
    assert truncation["was_truncated"] is True
    assert {
        (item["section"], item["returned"], item["total"])
        for item in truncation["sections"]
    } >= {("questions", 1, 2)}


def test_decision_context_route_resolves_question_anchor_by_id(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    import lab_tracker.decision_context_use_case as decision_context_use_case

    monkeypatch.setattr(decision_context_use_case, "CONTEXT_LOOKUP_LIMIT", 1)
    project_id = client.post(
        "/projects",
        json={"name": "Anchor Lookup Project", "description": ""},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Older anchor lookup question",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    )
    anchored_question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Newer anchor lookup question",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    ).json()["data"]["question_id"]

    response = client.post(
        "/assistant/decision-context",
        json={
            "task_kind": "summary",
            "query": "anchor lookup",
            "project_id": project_id,
            "question_id": anchored_question_id,
            "limit": 1,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["scope"]["anchors"][0]["entity_id"] == anchored_question_id
    assert data["questions"][0]["question_id"] == anchored_question_id
    assert "anchor" in data["questions"][0]["relevance_reasons"]


def test_decision_context_route_returns_newest_recent_activity(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = client.post(
        "/projects",
        json={"name": "Recent Context Project", "description": ""},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    for text in ["Older recent question", "Newer recent question"]:
        client.post(
            "/questions",
            json={
                "project_id": project_id,
                "text": text,
                "question_type": "descriptive",
                "status": "active",
            },
            headers=admin_auth_headers,
        )

    response = client.post(
        "/assistant/decision-context",
        json={
            "task_kind": "summary",
            "query": "unmatched phrase",
            "project_id": project_id,
            "limit": 1,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    questions = response.json()["data"]["questions"]
    assert [item["text"] for item in questions] == ["Newer recent question"]
    assert questions[0]["relevance_reasons"] == ["recent_activity"]


def test_decision_context_route_truncates_long_note_fields(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = client.post(
        "/projects",
        json={"name": "Long Note Context Project", "description": ""},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    raw_content = "needle " + ("x" * 1200)
    client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": raw_content,
            "status": "committed",
        },
        headers=admin_auth_headers,
    )

    response = client.post(
        "/assistant/decision-context",
        json={
            "task_kind": "summary",
            "query": "needle",
            "project_id": project_id,
            "limit": 5,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    note = response.json()["data"]["notes"][0]
    assert len(note["raw_content"]) == 1000
    assert note["raw_content"].endswith("...")
    assert note["truncated_fields"]["raw_content"] == {
        "original_length": len(raw_content),
        "returned_length": 1000,
    }


def test_decision_context_route_is_read_only(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = client.post(
        "/projects",
        json={"name": "Read Only Context Project", "description": ""},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    read_paths = [
        "/questions",
        "/notes",
        "/sessions",
        "/datasets",
        "/analyses",
        "/claims",
        "/visualizations",
        "/graph-drafts",
    ]

    def totals() -> dict[str, int]:
        return {
            path: client.get(
                path,
                params={"project_id": project_id},
                headers=admin_auth_headers,
            ).json()["meta"]["total"]
            for path in read_paths
        }

    before = totals()
    response = client.post(
        "/assistant/decision-context",
        json={
            "task_kind": "summary",
            "query": "unrecorded observation",
            "project_id": project_id,
        },
        headers=admin_auth_headers,
    )
    after = totals()

    assert response.status_code == 200
    assert after == before


def test_decision_context_route_returns_empty_context_for_no_matches_in_project(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = client.post(
        "/projects",
        json={"name": "No Match Project", "description": ""},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]

    response = client.post(
        "/assistant/decision-context",
        json={
            "task_kind": "summary",
            "query": "missing term",
            "project_id": project_id,
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["scope"]["project"]["project_id"] == project_id
    assert data["questions"] == []
    assert data["notes"] == []
    assert data["truncation"]["was_truncated"] is False
