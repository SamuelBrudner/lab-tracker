"""MCP next_questions paging and truncation reporting (review finding M33)."""

from __future__ import annotations

import uuid

import httpx
import pytest

from lab_tracker import mcp_api_client
from lab_tracker.mcp_api_client import LabTrackerAPIClient, LabTrackerAPIError, MCPSettings

PROJECT_ID = str(uuid.UUID(int=1))


def _page(rows: list[dict[str, object]], request: httpx.Request) -> httpx.Response:
    limit = int(request.url.params["limit"])
    offset = int(request.url.params["offset"])
    assert 1 <= limit <= 200, "the API rejects limits above 200"
    return httpx.Response(
        200,
        json={
            "data": rows[offset : offset + limit],
            "meta": {"limit": limit, "offset": offset, "total": len(rows)},
        },
    )


def _fixture_rows(
    *, question_count: int, claim_count: int
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    questions = [
        {
            "question_id": f"question-{index}",
            "project_id": PROJECT_ID,
            "text": f"Question {index}",
            "status": "active",
        }
        for index in range(question_count)
    ]
    goal_links = [
        {"entity_type": "question", "entity_id": row["question_id"], "link_status": "committed"}
        for row in questions
    ]
    goals = [
        {
            "goal_id": "goal-1",
            "project_id": PROJECT_ID,
            "title": "Paper",
            "status": "planned",
            "links": goal_links,
        }
    ]
    # Every claim answers the *first* question only once the newest claim is seen.
    claims = [
        {
            "claim_id": f"claim-{index}",
            "status": "supported",
            "answers_question_ids": ["question-0"] if index == claim_count - 1 else [],
        }
        for index in range(claim_count)
    ]
    return goals, questions, claims


def _client(
    goals: list[dict[str, object]],
    questions: list[dict[str, object]],
    claims: list[dict[str, object]],
    requests: list[httpx.Request],
) -> LabTrackerAPIClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == f"/projects/{PROJECT_ID}/goals":
            status = request.url.params.get("status")
            return _page([row for row in goals if row["status"] == status], request)
        if path == "/questions":
            status = request.url.params.get("status")
            return _page([row for row in questions if row["status"] == status], request)
        if path == "/claims":
            status = request.url.params.get("status")
            return _page(
                [row for row in claims if status is None or row["status"] == status],
                request,
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    return LabTrackerAPIClient(
        MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )


def test_next_questions_pages_past_the_200_row_api_cap() -> None:
    goals, questions, claims = _fixture_rows(question_count=250, claim_count=250)
    requests: list[httpx.Request] = []
    client = _client(goals, questions, claims, requests)
    try:
        payload = client.next_questions(project_id=PROJECT_ID, limit=20)
    finally:
        client.close()

    ranked_ids = {item["question"]["question_id"] for item in payload["data"]}
    # question-0 is answered by the newest (250th) claim, so it must not be ranked.
    assert "question-0" not in ranked_ids
    # Every other question beyond row 200 is still a candidate.
    assert payload["meta"]["total_candidates"] == 249
    assert payload["meta"]["inputs_truncated"] is False
    assert payload["meta"]["truncated_inputs"] == []
    claim_requests = [request for request in requests if request.url.path == "/claims"]
    assert [request.url.params["offset"] for request in claim_requests] == ["0", "200"]
    # Only supported claims can answer a question, so the fetch filters on them.
    assert {request.url.params["status"] for request in claim_requests} == {"supported"}


def test_next_questions_reports_truncated_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_api_client, "NEXT_QUESTIONS_MAX_ROWS_PER_LIST", 3)
    goals, questions, claims = _fixture_rows(question_count=5, claim_count=4)
    client = _client(goals, questions, claims, [])
    try:
        payload = client.next_questions(project_id=PROJECT_ID)
    finally:
        client.close()

    assert payload["meta"]["inputs_truncated"] is True
    truncated = {entry["list"]: entry for entry in payload["meta"]["truncated_inputs"]}
    assert truncated["questions"] == {
        "list": "questions",
        "project_id": PROJECT_ID,
        "status": "active",
        "fetched": 3,
        "total": 5,
    }
    assert truncated["claims"]["fetched"] == 3
    assert truncated["claims"]["total"] == 4
    assert "goals" not in truncated


def test_next_questions_requires_list_totals() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client = LabTrackerAPIClient(
        MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(LabTrackerAPIError, match="meta.total"):
            client.next_questions(project_id=PROJECT_ID)
    finally:
        client.close()
