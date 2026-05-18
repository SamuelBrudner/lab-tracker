from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any

from fastapi.testclient import TestClient


class FakeDraftClient:
    model = "fake-gpt"

    def __init__(self, patch: dict[str, Any]) -> None:
        self.patch = patch
        self.calls: list[dict[str, Any]] = []
        self.analysis_calls: list[dict[str, Any]] = []
        self.closed = False

    def draft_from_image(
        self,
        *,
        image_bytes: bytes,
        content_type: str,
        project_context: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "content_type": content_type,
                "image_bytes": image_bytes,
                "project_context": project_context,
            }
        )
        return self.patch

    def draft_from_analysis_evidence(
        self,
        *,
        evidence_text: str,
        project_context: dict[str, Any],
    ) -> dict[str, Any]:
        self.analysis_calls.append(
            {
                "evidence_text": evidence_text,
                "project_context": project_context,
            }
        )
        return self.patch

    def close(self) -> None:
        self.closed = True


def _project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/projects", json={"name": "Daily Review Project"}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _draft_patch(project_id: str) -> dict[str, Any]:
    return {
        "summary": "Drafted daily update",
        "operations": [
            {
                "client_ref": "daily_question",
                "op": "create",
                "entity_type": "question",
                "target_entity_id": None,
                "payload_json": json.dumps(
                    {
                        "project_id": project_id,
                        "text": "Does the daily review preserve context?",
                        "question_type": "descriptive",
                        "status": "staged",
                    }
                ),
                "rationale": "The day's evidence asks a trackable question.",
                "confidence": 0.8,
                "source_refs": [{"label": "daily note", "quote": "preserve context"}],
            }
        ],
    }


def test_daily_graph_review_generates_and_reuses_note_drafts(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    image = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("bench.jpg", b"fake-image", "image/jpeg")},
        headers=admin_auth_headers,
    )
    assert image.status_code == 201
    text = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Voice note transcript: cells looked healthier after media change.",
            "status": "committed",
        },
        headers=admin_auth_headers,
    )
    assert text.status_code == 201

    fake_client = FakeDraftClient(_draft_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client
    window_start = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    window_end = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()

    created = client.post(
        "/daily-graph-reviews",
        json={
            "project_id": project_id,
            "window_start": window_start,
            "window_end": window_end,
        },
        headers=admin_auth_headers,
    )

    assert created.status_code == 201
    review = created.json()["data"]
    assert review["status"] == "ready"
    assert len(review["change_set_ids"]) == 2
    assert "2 evidence notes" in review["summary"]
    assert len(fake_client.calls) == 1
    assert len(fake_client.analysis_calls) == 1
    assert "cells looked healthier" in fake_client.analysis_calls[0]["evidence_text"]

    duplicate = client.post(
        "/daily-graph-reviews",
        json={
            "project_id": project_id,
            "window_start": window_start,
            "window_end": window_end,
        },
        headers=admin_auth_headers,
    )

    assert duplicate.status_code == 201
    assert duplicate.json()["data"]["review_id"] == review["review_id"]
    assert len(fake_client.calls) == 1
    assert len(fake_client.analysis_calls) == 1

    listed = client.get(
        f"/daily-graph-reviews?project_id={project_id}&status=ready",
        headers=admin_auth_headers,
    )
    assert listed.status_code == 200
    assert listed.json()["data"][0]["review_id"] == review["review_id"]


def test_daily_graph_review_can_be_marked_reviewed(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    fake_client = FakeDraftClient({"summary": "empty", "operations": []})
    client.app.state.graph_draft_client_factory = lambda settings: fake_client
    window_start = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    window_end = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    review = client.post(
        "/daily-graph-reviews",
        json={
            "project_id": project_id,
            "window_start": window_start,
            "window_end": window_end,
        },
        headers=admin_auth_headers,
    ).json()["data"]

    marked = client.post(
        f"/daily-graph-reviews/{review['review_id']}/reviewed",
        headers=admin_auth_headers,
    )

    assert marked.status_code == 200
    payload = marked.json()["data"]
    assert payload["status"] == "reviewed"
    assert payload["reviewed_at"] is not None
    assert payload["reviewed_by"] is not None
