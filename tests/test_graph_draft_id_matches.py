"""Rule-based id-match link proposals in the daily batch draft."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from lab_tracker.services.graph_draft_id_matches import ID_MATCH_BASIS

COMMIT = "9f2c1d4e5a6b7c8d9e0f1a2b3c4d5e6f70819203"


class _FakeBatchClient:
    provider = "fake"
    model = "fake-batch-model"

    def __init__(self, patch: dict[str, Any] | None = None) -> None:
        self.patch = patch or {
            "summary": "nothing from the model",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [],
        }

    def draft_from_batch(self, *, batch_context: dict[str, Any], user_hint: str | None = None):
        return self.patch

    def close(self) -> None:
        pass


def _project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post(
        "/projects", json={"name": f"Id match {uuid4().hex[:6]}"}, headers=headers
    )
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _question(client: TestClient, headers: dict[str, str], project_id: str) -> str:
    response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does the id match land?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["question_id"]


def _committed_dataset(client: TestClient, headers: dict[str, str], project_id: str) -> str:
    question_id = _question(client, headers, project_id)
    response = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "status": "committed",
            "commit_manifest": {
                "external_artifacts": [
                    {
                        "kind": "entity",
                        "source_system": "s3",
                        "uri": "s3://lab-bucket/run-001/manifest.json",
                        "content_hash": "sha256:manifest001",
                    }
                ]
            },
        },
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["dataset_id"]


def _committed_analysis(
    client: TestClient, headers: dict[str, str], project_id: str, *, code_version: str
) -> str:
    dataset_id = _committed_dataset(client, headers, project_id)
    response = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": f"method-{uuid4().hex[:8]}",
            "code_version": code_version,
            "status": "committed",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["analysis_id"]


def _session(client: TestClient, headers: dict[str, str], project_id: str) -> str:
    response = client.post(
        "/sessions",
        json={"project_id": project_id, "session_type": "operational"},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["session_id"]


def _note(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    text: str,
    *,
    metadata: dict[str, str] | None = None,
    targets: list[dict[str, str]] | None = None,
) -> str:
    payload: dict[str, Any] = {"project_id": project_id, "raw_content": text, "status": "staged"}
    if metadata:
        payload["metadata"] = metadata
    if targets:
        payload["targets"] = targets
    response = client.post("/notes", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["note_id"]


def _run_batch(client: TestClient, headers: dict[str, str], project_id: str) -> dict[str, Any]:
    client.app.state.graph_draft_client_factory = lambda settings: _FakeBatchClient()
    run = client.post("/batches/run-now", json={"project_id": project_id}, headers=headers)
    assert run.status_code == 201, run.text
    change_set_id = run.json()["data"]["change_set_id"]
    draft = client.get(f"/batches/{change_set_id}", headers=headers)
    assert draft.status_code == 200
    return draft.json()["data"]


def test_batch_draft_proposes_exact_id_links_without_the_model(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    analysis_id = _committed_analysis(client, admin_auth_headers, project_id, code_version=COMMIT)
    session_id = _session(client, admin_auth_headers, project_id)
    note_id = _note(
        client,
        admin_auth_headers,
        project_id,
        "Figure saved from the analysis run",
        metadata={"run_git_commit": COMMIT[:12], "watch_session_id": session_id},
    )
    plain_note_id = _note(client, admin_auth_headers, project_id, "No ids here")

    draft = _run_batch(client, admin_auth_headers, project_id)

    assert draft["status"] == "ready"
    assert draft["context_packet"]["deterministic_id_matches"]["basis"] == ID_MATCH_BASIS
    proposed = draft["context_packet"]["deterministic_id_matches"]["proposed"]
    assert [item["note_id"] for item in proposed] == [note_id]
    operations = draft["operations"]
    assert len(operations) == 1
    operation = operations[0]
    assert operation["op"] == "update"
    assert operation["entity_type"] == "note"
    assert operation["semantic_type"] == "link_note_to_session"
    assert operation["target_entity_id"] == note_id
    assert operation["confidence"] == 1.0
    assert operation["status"] == "proposed"
    assert "Exact id match" in operation["rationale"]
    assert {(t["entity_type"], t["entity_id"]) for t in operation["payload"]["targets"]} == {
        ("session", session_id),
        ("analysis", analysis_id),
    }
    assert all(ref["basis"] == ID_MATCH_BASIS for ref in operation["source_refs"])
    assert all(ref["source_note_ids"] == [note_id] for ref in operation["source_refs"])
    assert {ref["quote"] for ref in operation["source_refs"]} == {
        f"run_git_commit={COMMIT[:12]}",
        f"watch_session_id={session_id}",
    }
    assert plain_note_id not in json.dumps(operations)

    # The proposal commits through the ordinary review gate and links the note.
    accept = client.patch(
        f"/graph-drafts/{draft['change_set_id']}/operations/{operation['operation_id']}",
        json={"status": "accepted"},
        headers=admin_auth_headers,
    )
    assert accept.status_code == 200, accept.text
    commit = client.post(
        f"/graph-drafts/{draft['change_set_id']}/commit",
        json={"message": "link by id"},
        headers=admin_auth_headers,
    )
    assert commit.status_code == 200, commit.text
    note = client.get(f"/notes/{note_id}", headers=admin_auth_headers).json()["data"]
    assert {(t["entity_type"], t["entity_id"]) for t in note["targets"]} == {
        ("session", session_id),
        ("analysis", analysis_id),
    }


def test_id_match_pass_keeps_existing_targets_and_stays_silent_when_unsure(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    session_id = _session(client, admin_auth_headers, project_id)
    question_id = _question(client, admin_auth_headers, project_id)
    # Two committed analyses share a prefix: the commit is ambiguous.
    _committed_analysis(client, admin_auth_headers, project_id, code_version=COMMIT)
    _committed_analysis(client, admin_auth_headers, project_id, code_version=COMMIT[:20] + "ffff")
    already_linked = _note(
        client,
        admin_auth_headers,
        project_id,
        "Already attached to its session",
        metadata={"watch_session_id": session_id},
        targets=[{"entity_type": "session", "entity_id": session_id}],
    )
    ambiguous = _note(
        client,
        admin_auth_headers,
        project_id,
        "Commit prefix matches two analyses",
        metadata={"repo_git_commit": COMMIT[:12]},
    )
    too_short = _note(
        client,
        admin_auth_headers,
        project_id,
        "Commit too short to trust",
        metadata={"git_commit": COMMIT[:5]},
    )
    keeps_question = _note(
        client,
        admin_auth_headers,
        project_id,
        "Session id plus an existing question link",
        metadata={"capture_session_id": session_id},
        targets=[{"entity_type": "question", "entity_id": question_id}],
    )

    draft = _run_batch(client, admin_auth_headers, project_id)

    operations = draft["operations"]
    assert [op["target_entity_id"] for op in operations] == [keeps_question]
    targets = operations[0]["payload"]["targets"]
    # The existing question link rides along: a note update replaces targets.
    assert {(t["entity_type"], t["entity_id"]) for t in targets} == {
        ("question", question_id),
        ("session", session_id),
    }
    dumped = json.dumps(operations)
    assert already_linked not in dumped
    assert ambiguous not in dumped
    assert too_short not in dumped
    assert draft["context_packet"]["deterministic_id_matches"]["skipped"] == []
