from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lab_tracker.auth import Role


def _ids(payload: list[dict[str, object]], key: str) -> set[str]:
    return {str(item[key]) for item in payload}


def _assert_source_file_metadata(
    metadata: dict[str, object],
    *,
    filename: str,
    content_type: str,
    size_bytes: int,
) -> None:
    assert metadata["source_file_name"] == filename
    assert metadata["source_file_content_type"] == content_type
    assert metadata["source_file_size_bytes"] == str(size_bytes)
    assert isinstance(metadata["source_file_checksum"], str)
    assert len(str(metadata["source_file_checksum"])) == 64
    datetime.fromisoformat(str(metadata["source_file_ingested_at"]))


def _admin_headers(client: TestClient) -> dict[str, str]:
    username = f"admin-{uuid4().hex[:8]}"
    password = "secret"
    client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=Role.ADMIN,
    )
    response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    token = response.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _fail_next_commit(monkeypatch) -> None:
    original_commit = Session.commit
    state = {"failed": False}

    def _commit_once(self, *args, **kwargs):  # noqa: ANN001, ANN202
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("forced commit failure")
        return original_commit(self, *args, **kwargs)

    monkeypatch.setattr(Session, "commit", _commit_once)


def test_core_entity_crud_routes_use_database_persistence(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers

    project_response = client.post(
        "/projects",
        json={"name": "CRUD Integration", "description": "initial"},
        headers=headers,
    )
    assert project_response.status_code == 201
    project_id = project_response.json()["data"]["project_id"]

    project_get_response = client.get(f"/projects/{project_id}", headers=headers)
    assert project_get_response.status_code == 200
    assert project_get_response.json()["data"]["name"] == "CRUD Integration"

    project_list_response = client.get("/projects", headers=headers)
    assert project_list_response.status_code == 200
    assert project_id in _ids(project_list_response.json()["data"], "project_id")

    project_update_response = client.patch(
        f"/projects/{project_id}",
        json={"description": "updated"},
        headers=headers,
    )
    assert project_update_response.status_code == 200
    assert project_update_response.json()["data"]["description"] == "updated"

    question_create_response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does the integration path persist?",
            "question_type": "descriptive",
        },
        headers=headers,
    )
    assert question_create_response.status_code == 201
    question_id = question_create_response.json()["data"]["question_id"]

    question_get_response = client.get(f"/questions/{question_id}", headers=headers)
    assert question_get_response.status_code == 200
    assert question_get_response.json()["data"]["project_id"] == project_id

    question_list_response = client.get(
        "/questions",
        params={"project_id": project_id},
        headers=headers,
    )
    assert question_list_response.status_code == 200
    assert question_id in _ids(question_list_response.json()["data"], "question_id")

    question_update_response = client.patch(
        f"/questions/{question_id}",
        json={"status": "active", "text": "Does the database-backed API persist?"},
        headers=headers,
    )
    assert question_update_response.status_code == 200
    assert question_update_response.json()["data"]["status"] == "active"

    dataset_create_response = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
        },
        headers=headers,
    )
    assert dataset_create_response.status_code == 201
    dataset_id = dataset_create_response.json()["data"]["dataset_id"]

    dataset_get_response = client.get(f"/datasets/{dataset_id}", headers=headers)
    assert dataset_get_response.status_code == 200
    assert dataset_get_response.json()["data"]["project_id"] == project_id

    dataset_list_response = client.get(
        "/datasets",
        params={"project_id": project_id},
        headers=headers,
    )
    assert dataset_list_response.status_code == 200
    assert dataset_id in _ids(dataset_list_response.json()["data"], "dataset_id")

    file_upload_response = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("data.bin", b"integration-test-data", "application/octet-stream")},
        headers=headers,
    )
    assert file_upload_response.status_code == 201

    dataset_update_response = client.patch(
        f"/datasets/{dataset_id}",
        json={"status": "committed"},
        headers=headers,
    )
    assert dataset_update_response.status_code == 200
    assert dataset_update_response.json()["data"]["status"] == "committed"

    note_create_response = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "capture log"},
        headers=headers,
    )
    assert note_create_response.status_code == 201
    note_id = note_create_response.json()["data"]["note_id"]

    note_get_response = client.get(f"/notes/{note_id}", headers=headers)
    assert note_get_response.status_code == 200
    assert note_get_response.json()["data"]["project_id"] == project_id

    note_list_response = client.get(
        "/notes",
        params={"project_id": project_id},
        headers=headers,
    )
    assert note_list_response.status_code == 200
    assert note_id in _ids(note_list_response.json()["data"], "note_id")

    note_update_response = client.patch(
        f"/notes/{note_id}",
        json={
            "transcribed_text": "transcribed capture",
            "metadata": {
                "source": "integration",
                "run": 7,
                "verified": True,
                "score": 1.5,
            },
        },
        headers=headers,
    )
    assert note_update_response.status_code == 200
    note_payload = note_update_response.json()["data"]
    assert note_payload["transcribed_text"] == "transcribed capture"
    assert note_payload["metadata"] == {
        "source": "integration",
        "run": "7",
        "verified": "True",
        "score": "1.5",
    }

    note_delete_response = client.delete(f"/notes/{note_id}", headers=headers)
    assert note_delete_response.status_code == 200
    assert client.get(f"/notes/{note_id}", headers=headers).status_code == 404

    dataset_delete_response = client.delete(f"/datasets/{dataset_id}", headers=headers)
    assert dataset_delete_response.status_code == 200
    assert client.get(f"/datasets/{dataset_id}", headers=headers).status_code == 404

    question_delete_response = client.delete(f"/questions/{question_id}", headers=headers)
    assert question_delete_response.status_code == 200
    assert client.get(f"/questions/{question_id}", headers=headers).status_code == 404

    project_delete_response = client.delete(f"/projects/{project_id}", headers=headers)
    assert project_delete_response.status_code == 200
    assert client.get(f"/projects/{project_id}", headers=headers).status_code == 404


def test_analysis_commit_route_is_atomic_on_failure(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers

    project_id = client.post(
        "/projects",
        json={"name": "Atomic analysis"},
        headers=headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does atomic commit hold?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    dataset_id = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
        },
        headers=headers,
    ).json()["data"]["dataset_id"]
    upload_response = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("data.bin", b"atomic-test", "application/octet-stream")},
        headers=headers,
    )
    assert upload_response.status_code == 201
    commit_dataset = client.patch(
        f"/datasets/{dataset_id}",
        json={"status": "committed"},
        headers=headers,
    )
    assert commit_dataset.status_code == 200

    analysis_response = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "method-1",
            "code_version": "v1",
        },
        headers=headers,
    )
    assert analysis_response.status_code == 201
    analysis_id = analysis_response.json()["data"]["analysis_id"]

    commit_response = client.post(
        f"/analyses/{analysis_id}/commit",
        json={
            "environment_hash": "env-1",
            "claims": [
                {
                    "statement": "Signal is stable",
                    "confidence": 0.8,
                    "status": "supported",
                }
            ],
            "visualizations": [
                {
                    "viz_type": "line",
                    "file_path": "figs/signal.png",
                    "related_claim_ids": ["11111111-1111-1111-1111-111111111111"],
                }
            ],
        },
        headers=headers,
    )
    assert commit_response.status_code == 404

    analysis_get = client.get(f"/analyses/{analysis_id}", headers=headers)
    claims_get = client.get("/claims", params={"project_id": project_id}, headers=headers)
    visualizations_get = client.get(
        "/visualizations",
        params={"analysis_id": analysis_id},
        headers=headers,
    )

    assert analysis_get.status_code == 200
    assert analysis_get.json()["data"]["status"] == "staged"
    assert claims_get.status_code == 200
    assert claims_get.json()["data"] == []
    assert visualizations_get.status_code == 200
    assert visualizations_get.json()["data"] == []


def test_evidence_authoring_routes_create_and_filter_graph_records(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers

    project_id = client.post(
        "/projects",
        json={"name": "Evidence authoring"},
        headers=headers,
    ).json()["data"]["project_id"]
    primary_question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Which result should be preserved?",
            "question_type": "descriptive",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    secondary_question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Which control supports the result?",
            "question_type": "method_dev",
        },
        headers=headers,
    ).json()["data"]["question_id"]

    dataset_response = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": primary_question_id,
            "secondary_question_ids": [secondary_question_id],
            "status": "staged",
        },
        headers=headers,
    )
    assert dataset_response.status_code == 201
    dataset_payload = dataset_response.json()["data"]
    dataset_id = dataset_payload["dataset_id"]
    assert dataset_payload["status"] == "staged"
    assert {
        (link["question_id"], link["role"]) for link in dataset_payload["question_links"]
    } == {
        (primary_question_id, "primary"),
        (secondary_question_id, "secondary"),
    }

    analysis_response = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "publication:eLife-2021-vae-feature-space",
            "code_version": "published-pdf:elife-67855-v2",
        },
        headers=headers,
    )
    assert analysis_response.status_code == 201
    analysis_id = analysis_response.json()["data"]["analysis_id"]

    proposed_claim_response = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "Juvenile song syllables occupy a structured feature space.",
            "confidence": 65.0,
            "status": "proposed",
        },
        headers=headers,
    )
    assert proposed_claim_response.status_code == 201
    proposed_claim_id = proposed_claim_response.json()["data"]["claim_id"]

    supported_claim_response = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "The analysis supports the retained figure interpretation.",
            "confidence": 86.0,
            "status": "supported",
            "supported_by_analysis_ids": [analysis_id],
        },
        headers=headers,
    )
    assert supported_claim_response.status_code == 201
    supported_claim_id = supported_claim_response.json()["data"]["claim_id"]

    visualization_response = client.post(
        "/visualizations",
        json={
            "analysis_id": analysis_id,
            "viz_type": "paper_figure",
            "file_path": "doi:10.1371/journal.pcbi.1011051#fig5",
            "caption": "Published figure locator for retrospective evidence.",
            "related_claim_ids": [supported_claim_id],
        },
        headers=headers,
    )
    assert visualization_response.status_code == 201
    visualization_id = visualization_response.json()["data"]["viz_id"]

    claim_note_response = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Source note for supported claim.",
            "targets": [{"entity_type": "claim", "entity_id": supported_claim_id}],
        },
        headers=headers,
    )
    assert claim_note_response.status_code == 201
    visualization_note_response = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Source note for visualization.",
            "targets": [
                {"entity_type": "visualization", "entity_id": visualization_id}
            ],
        },
        headers=headers,
    )
    assert visualization_note_response.status_code == 201

    datasets = client.get(
        "/datasets",
        params={"project_id": project_id, "status": "staged"},
        headers=headers,
    )
    analyses = client.get(
        "/analyses",
        params={"project_id": project_id, "dataset_id": dataset_id},
        headers=headers,
    )
    claims = client.get("/claims", params={"project_id": project_id}, headers=headers)
    visualizations = client.get(
        "/visualizations",
        params={"project_id": project_id, "claim_id": supported_claim_id},
        headers=headers,
    )
    claim_notes = client.get(
        "/notes",
        params={
            "project_id": project_id,
            "target_entity_type": "claim",
            "target_entity_id": supported_claim_id,
        },
        headers=headers,
    )
    visualization_notes = client.get(
        "/notes",
        params={
            "project_id": project_id,
            "target_entity_type": "visualization",
            "target_entity_id": visualization_id,
        },
        headers=headers,
    )

    assert dataset_id in _ids(datasets.json()["data"], "dataset_id")
    assert analysis_id in _ids(analyses.json()["data"], "analysis_id")
    assert {proposed_claim_id, supported_claim_id}.issubset(
        _ids(claims.json()["data"], "claim_id")
    )
    assert visualization_id in _ids(visualizations.json()["data"], "viz_id")
    assert claim_notes.json()["data"][0]["targets"] == [
        {"entity_type": "claim", "entity_id": supported_claim_id}
    ]
    assert visualization_notes.json()["data"][0]["targets"] == [
        {"entity_type": "visualization", "entity_id": visualization_id}
    ]


def test_evidence_authoring_routes_reject_invalid_graph_writes(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers

    project_id = client.post(
        "/projects",
        json={"name": "Evidence validation"},
        headers=headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Which invalid writes are rejected?",
            "question_type": "descriptive",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    dataset_id = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    ).json()["data"]["dataset_id"]
    analysis_id = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "method-1",
            "code_version": "v1",
        },
        headers=headers,
    ).json()["data"]["analysis_id"]

    supported_without_evidence = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "Unsupported supported claim",
            "confidence": 80.0,
            "status": "supported",
        },
        headers=headers,
    )
    assert supported_without_evidence.status_code == 422
    assert "Supported claims require" in supported_without_evidence.json()["error"]["message"]

    visualization_with_unknown_claim = client.post(
        "/visualizations",
        json={
            "analysis_id": analysis_id,
            "viz_type": "line",
            "file_path": "figs/line.png",
            "related_claim_ids": ["11111111-1111-1111-1111-111111111111"],
        },
        headers=headers,
    )
    assert visualization_with_unknown_claim.status_code == 404

    committed_dataset_without_files = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "status": "committed",
        },
        headers=headers,
    )
    assert committed_dataset_without_files.status_code == 422
    assert "At least one file" in committed_dataset_without_files.json()["error"]["message"]


def test_question_list_paginates_beyond_200_records(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    project_id = client.post(
        "/projects",
        json={"name": "Pagination Project"},
        headers=headers,
    ).json()["data"]["project_id"]

    for index in range(205):
        response = client.post(
            "/questions",
            json={
                "project_id": project_id,
                "text": f"Paginated question {index}",
                "question_type": "descriptive",
            },
            headers=headers,
        )
        assert response.status_code == 201

    page = client.get(
        "/questions",
        params={"project_id": project_id, "limit": 200, "offset": 200},
        headers=headers,
    )
    assert page.status_code == 200
    payload = page.json()
    assert payload["meta"] == {"limit": 200, "offset": 200, "total": 205}
    assert len(payload["data"]) == 5


def test_question_routes_store_and_filter_hierarchy(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    project_id = client.post(
        "/projects",
        json={"name": "Question hierarchy"},
        headers=headers,
    ).json()["data"]["project_id"]
    other_project_id = client.post(
        "/projects",
        json={"name": "Other hierarchy project"},
        headers=headers,
    ).json()["data"]["project_id"]

    root_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "What motivates this experiment?",
            "question_type": "descriptive",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    child_response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Which atomic behavior changes?",
            "question_type": "hypothesis_driven",
            "parent_question_ids": [root_id],
        },
        headers=headers,
    )
    assert child_response.status_code == 201
    child_payload = child_response.json()["data"]
    child_id = child_payload["question_id"]
    assert child_payload["parent_question_ids"] == [root_id]

    grandchild_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Which control explains the change?",
            "question_type": "method_dev",
            "parent_question_ids": [child_id],
        },
        headers=headers,
    ).json()["data"]["question_id"]

    direct_children = client.get(
        "/questions",
        params={"project_id": project_id, "parent_question_id": root_id},
        headers=headers,
    )
    assert direct_children.status_code == 200
    assert _ids(direct_children.json()["data"], "question_id") == {child_id}

    descendants = client.get(
        "/questions",
        params={"project_id": project_id, "ancestor_question_id": root_id},
        headers=headers,
    )
    assert descendants.status_code == 200
    assert _ids(descendants.json()["data"], "question_id") == {child_id, grandchild_id}

    other_question_id = client.post(
        "/questions",
        json={
            "project_id": other_project_id,
            "text": "A question from another project",
            "question_type": "descriptive",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    cross_project_response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Should cross-project parents fail?",
            "question_type": "other",
            "parent_question_ids": [other_question_id],
        },
        headers=headers,
    )
    assert cross_project_response.status_code == 422

    cycle_response = client.patch(
        f"/questions/{root_id}",
        json={"parent_question_ids": [grandchild_id]},
        headers=headers,
    )
    assert cycle_response.status_code == 422


def test_question_refactor_routes_persist_supersession_and_relationship_moves(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    project_id = client.post(
        "/projects",
        json={"name": "Question refactor routes"},
        headers=headers,
    ).json()["data"]["project_id"]
    source_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does this broad question need refactoring?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    child_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Which child should follow the refined question?",
            "question_type": "method_dev",
            "parent_question_ids": [source_id],
        },
        headers=headers,
    ).json()["data"]["question_id"]
    note_id = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Move this interpretation to the refined question.",
            "targets": [{"entity_type": "question", "entity_id": source_id}],
        },
        headers=headers,
    ).json()["data"]["note_id"]

    missing_reason = client.post(
        f"/questions/{source_id}/refactor",
        json={
            "replacement": {
                "text": "Missing reason replacement",
                "question_type": "descriptive",
                "status": "staged",
            },
        },
        headers=headers,
    )
    assert missing_reason.status_code == 422

    response = client.post(
        f"/questions/{source_id}/refactor",
        json={
            "replacement": {
                "text": "Which concrete comparison should this project test?",
                "question_type": "hypothesis_driven",
                "status": "active",
            },
            "reason": "Make the project question testable before work begins.",
            "child_question_ids_to_reparent": [child_id],
            "note_ids_to_retarget": [note_id],
        },
        headers=headers,
    )

    assert response.status_code == 201
    payload = response.json()["data"]
    replacement_id = payload["replacement_question"]["question_id"]
    assert payload["source_question"]["status"] == "superseded"
    assert payload["source_question"]["superseded_by_question_id"] == replacement_id
    assert payload["replacement_question"]["supersedes_question_id"] == source_id
    assert payload["refactor"]["source_question_id"] == source_id
    assert payload["refactor"]["replacement_question_id"] == replacement_id
    assert payload["refactor"]["relationship_changes"] == {
        "child_question_ids_reparented": [child_id],
        "note_ids_retargeted": [note_id],
        "dataset_session_analysis_claim_links_moved": False,
    }

    child = client.get(f"/questions/{child_id}", headers=headers)
    assert child.status_code == 200
    assert child.json()["data"]["parent_question_ids"] == [replacement_id]
    note = client.get(f"/notes/{note_id}", headers=headers)
    assert note.status_code == 200
    assert note.json()["data"]["targets"] == [
        {"entity_type": "question", "entity_id": replacement_id}
    ]
    source_history = client.get(f"/questions/{source_id}/refactors", headers=headers)
    replacement_history = client.get(f"/questions/{replacement_id}/refactors", headers=headers)
    assert source_history.status_code == 200
    assert replacement_history.status_code == 200
    assert source_history.json()["data"][0]["refactor_id"] == payload["refactor"]["refactor_id"]
    assert (
        replacement_history.json()["data"][0]["refactor_id"]
        == payload["refactor"]["refactor_id"]
    )


def test_note_routes_support_target_filters_and_multipart_upload(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    project_id = client.post(
        "/projects",
        json={"name": "Notes Project"},
        headers=headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Which dataset note is linked?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    dataset_id = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
        },
        headers=headers,
    ).json()["data"]["dataset_id"]

    multipart_upload = client.post(
        "/notes/upload-file",
        data={
            "project_id": project_id,
            "transcribed_text": "typed capture",
            "targets": json.dumps(
                [
                    {
                        "entity_id": dataset_id,
                        "entity_type": "dataset",
                    }
                ]
            ),
            "metadata": json.dumps(
                {
                    "source": "camera",
                    "source_file_created_at": "2026-04-20T01:02:03Z",
                    "source_file_last_modified_ms": 1769904000000,
                }
            ),
        },
        files={"file": ("capture.txt", b"raw-capture", "text/plain")},
        headers=headers,
    )
    assert multipart_upload.status_code == 201
    multipart_payload = multipart_upload.json()["data"]
    assert multipart_payload["transcribed_text"] == "typed capture"
    assert multipart_payload["metadata"]["source"] == "camera"
    assert (
        multipart_payload["metadata"]["source_file_created_at"]
        == "2026-04-20T01:02:03+00:00"
    )
    assert multipart_payload["metadata"]["source_file_last_modified_ms"] == "1769904000000"
    assert (
        multipart_payload["metadata"]["source_file_last_modified_at"]
        == "2026-02-01T00:00:00+00:00"
    )
    _assert_source_file_metadata(
        multipart_payload["metadata"],
        filename="capture.txt",
        content_type="text/plain",
        size_bytes=len(b"raw-capture"),
    )
    assert multipart_payload["raw_asset"]["filename"] == "capture.txt"
    assert multipart_payload["targets"][0]["entity_type"] == "dataset"
    assert multipart_payload["targets"][0]["entity_id"] == dataset_id

    raw_download = client.get(
        f"/notes/{multipart_payload['note_id']}/raw",
        headers=headers,
    )
    assert raw_download.status_code == 200
    assert raw_download.content == b"raw-capture"

    filtered = client.get(
        "/notes",
        params={
            "project_id": project_id,
            "target_entity_type": "dataset",
            "target_entity_id": dataset_id,
        },
        headers=headers,
    )
    assert filtered.status_code == 200
    filtered_payload = filtered.json()
    assert filtered_payload["meta"]["total"] == 1
    assert _ids(filtered_payload["data"], "note_id") == {multipart_payload["note_id"]}


def test_removed_note_upload_route_returns_404(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    response = client.post("/notes/upload", json={}, headers=admin_auth_headers)
    assert response.status_code == 404


def test_note_multipart_upload_cleans_up_raw_asset_on_failure(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    storage_root = Path(client.app.state.raw_note_storage._base_path)
    before = sorted(path.name for path in storage_root.iterdir()) if storage_root.exists() else []

    response = client.post(
        "/notes/upload-file",
        data={"project_id": str(uuid4())},
        files={"file": ("capture.txt", b"raw-capture", "text/plain")},
        headers=admin_auth_headers,
    )

    assert response.status_code == 404
    after = sorted(path.name for path in storage_root.iterdir()) if storage_root.exists() else []
    assert after == before


def test_note_multipart_upload_rejects_invalid_targets_without_leaking_raw_asset(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    project_id = client.post(
        "/projects",
        json={"name": "Invalid targets cleanup"},
        headers=headers,
    ).json()["data"]["project_id"]
    storage_root = Path(client.app.state.raw_note_storage._base_path)
    before = sorted(path.name for path in storage_root.iterdir()) if storage_root.exists() else []

    response = client.post(
        "/notes/upload-file",
        data={
            "project_id": project_id,
            "targets": "{not json",
        },
        files={"file": ("capture.txt", b"raw-capture", "text/plain")},
        headers=headers,
    )

    assert response.status_code == 422
    after = sorted(path.name for path in storage_root.iterdir()) if storage_root.exists() else []
    assert after == before


def test_note_multipart_upload_rejects_invalid_metadata_without_leaking_raw_asset(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    project_id = client.post(
        "/projects",
        json={"name": "Invalid metadata cleanup"},
        headers=headers,
    ).json()["data"]["project_id"]
    storage_root = Path(client.app.state.raw_note_storage._base_path)
    before = sorted(path.name for path in storage_root.iterdir()) if storage_root.exists() else []

    response = client.post(
        "/notes/upload-file",
        data={
            "project_id": project_id,
            "metadata": '{"source": {"kind": "camera"}}',
        },
        files={"file": ("capture.txt", b"raw-capture", "text/plain")},
        headers=headers,
    )

    assert response.status_code == 422
    after = sorted(path.name for path in storage_root.iterdir()) if storage_root.exists() else []
    assert after == before


def test_note_multipart_upload_cleans_up_raw_asset_when_request_commit_fails(app, monkeypatch):
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = _admin_headers(client)
        project_id = client.post(
            "/projects",
            json={"name": "Multipart note upload rollback"},
            headers=headers,
        ).json()["data"]["project_id"]
        storage_root = Path(client.app.state.raw_note_storage._base_path)

        _fail_next_commit(monkeypatch)
        response = client.post(
            "/notes/upload-file",
            data={"project_id": project_id},
            files={"file": ("rollback.txt", b"rollback-note", "text/plain")},
            headers=headers,
        )

        assert response.status_code == 500
        remaining = (
            sorted(path.name for path in storage_root.iterdir())
            if storage_root.exists()
            else []
        )
        assert remaining == []

        listed = client.get("/notes", params={"project_id": project_id}, headers=headers)
        assert listed.status_code == 200
        assert listed.json()["meta"]["total"] == 0


def test_note_delete_preserves_raw_asset_when_request_commit_fails(app, monkeypatch):
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = _admin_headers(client)
        project_id = client.post(
            "/projects",
            json={"name": "Note delete rollback"},
            headers=headers,
        ).json()["data"]["project_id"]
        upload = client.post(
            "/notes/upload-file",
            data={"project_id": project_id},
            files={"file": ("rollback.txt", b"rollback-note", "text/plain")},
            headers=headers,
        )
        assert upload.status_code == 201
        note_id = upload.json()["data"]["note_id"]
        storage_root = Path(client.app.state.raw_note_storage._base_path)
        before = sorted(path.name for path in storage_root.iterdir())
        assert len(before) == 1

        _fail_next_commit(monkeypatch)
        response = client.delete(f"/notes/{note_id}", headers=headers)

        assert response.status_code == 500
        after = sorted(path.name for path in storage_root.iterdir())
        assert after == before

        loaded = client.get(f"/notes/{note_id}", headers=headers)
        assert loaded.status_code == 200


def test_write_routes_reject_unknown_and_removed_fields(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    project_id = client.post(
        "/projects",
        json={"name": "Audit project"},
        headers=headers,
    ).json()["data"]["project_id"]

    project_response = client.post(
        "/projects",
        json={"name": "Audit project", "created_by": "spoofed-user"},
        headers=headers,
    )
    assert project_response.status_code == 422

    question_response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Should actor identity be derived server side?",
            "question_type": "descriptive",
            "created_by": "spoofed-user",
            "source_provenance": "ocr://note-123",
        },
        headers=headers,
    )
    assert question_response.status_code == 422

    note_response = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "audit test note",
            "created_by": "spoofed-user",
            "extracted_entities": [{"label": "Neuron"}],
        },
        headers=headers,
    )
    assert note_response.status_code == 422

    session_response = client.post(
        "/sessions",
        json={
            "project_id": project_id,
            "session_type": "operational",
            "created_by": "spoofed-user",
            "status": "closed",
        },
        headers=headers,
    )
    assert session_response.status_code == 422


def test_analysis_create_rejects_unknown_fields(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    project_id = client.post(
        "/projects",
        json={"name": "Analysis audit"},
        headers=headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Can analysis authorship be spoofed?",
            "question_type": "descriptive",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    dataset_id = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    ).json()["data"]["dataset_id"]

    analysis_response = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "method-1",
            "code_version": "v1",
            "executed_by": "spoofed-user",
        },
        headers=headers,
    )
    assert analysis_response.status_code == 422


def test_project_delete_cleans_up_cascaded_attachments(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    project_id = client.post(
        "/projects",
        json={"name": "Project cleanup"},
        headers=headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does project delete clean attachments?",
            "question_type": "descriptive",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    dataset_id = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    ).json()["data"]["dataset_id"]
    file_upload = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("cleanup.bin", b"dataset-bytes", "application/octet-stream")},
        headers=headers,
    )
    assert file_upload.status_code == 201
    note_upload = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("capture.txt", b"note-bytes", "text/plain")},
        headers=headers,
    )
    assert note_upload.status_code == 201

    file_storage_root = Path(client.app.state.file_storage_backend.base_path)
    note_storage_root = Path(client.app.state.raw_note_storage._base_path)
    assert sorted(path.suffix for path in file_storage_root.rglob("*") if path.is_file()) == [
        ".bin",
        ".json",
    ]
    assert len([path for path in note_storage_root.iterdir() if path.is_file()]) == 1

    delete_response = client.delete(f"/projects/{project_id}", headers=headers)
    assert delete_response.status_code == 200
    assert client.get(f"/projects/{project_id}", headers=headers).status_code == 404
    assert list(file_storage_root.rglob("*.bin")) == []
    assert list(file_storage_root.rglob("*.json")) == []
    assert list(note_storage_root.iterdir()) == []


def test_note_raw_download_sanitizes_attachment_filename(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    project_id = client.post(
        "/projects",
        json={"name": "Filename sanitization"},
        headers=headers,
    ).json()["data"]["project_id"]

    upload_response = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ('../../bad"\r\nname.txt', b"raw-capture", "text/plain")},
        headers=headers,
    )
    assert upload_response.status_code == 201
    note_id = upload_response.json()["data"]["note_id"]

    raw_download = client.get(f"/notes/{note_id}/raw", headers=headers)
    assert raw_download.status_code == 200
    assert raw_download.content == b"raw-capture"
    assert raw_download.headers["content-disposition"] == (
        'attachment; filename="bad\'__name.txt"'
    )


def test_quick_capture_stages_note_with_minimal_payload(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    project_id = client.post(
        "/projects",
        json={"name": "Quick capture"},
        headers=headers,
    ).json()["data"]["project_id"]

    for filename, content, content_type in (
        ("snap.jpg", b"jpeg-bytes", "image/jpeg"),
        ("memo.m4a", b"audio-bytes", "audio/mp4"),
        ("note.txt", b"plain text capture", "text/plain"),
    ):
        response = client.post(
            "/notes/quick-capture",
            data={"project_id": project_id},
            files={"file": (filename, content, content_type)},
            headers=headers,
        )
        assert response.status_code == 202, response.text
        payload = response.json()["data"]
        assert payload["status"] == "staged"
        assert payload["project_id"] == project_id
        assert payload["raw_asset"]["filename"] == filename
        assert payload["raw_asset"]["content_type"] == content_type
        assert payload["transcribed_text"] is None
        assert payload["targets"] == []
        _assert_source_file_metadata(
            payload["metadata"],
            filename=filename,
            content_type=content_type,
            size_bytes=len(content),
        )

        raw_download = client.get(
            f"/notes/{payload['note_id']}/raw",
            headers=headers,
        )
        assert raw_download.status_code == 200
        assert raw_download.content == content


def test_quick_capture_rejects_missing_required_fields(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    project_id = client.post(
        "/projects",
        json={"name": "Quick capture validation"},
        headers=headers,
    ).json()["data"]["project_id"]

    missing_file = client.post(
        "/notes/quick-capture",
        data={"project_id": project_id},
        headers=headers,
    )
    assert missing_file.status_code == 422

    missing_project = client.post(
        "/notes/quick-capture",
        files={"file": ("snap.jpg", b"jpeg-bytes", "image/jpeg")},
        headers=headers,
    )
    assert missing_project.status_code == 422


def test_quick_capture_preserves_metadata_but_ignores_workflow_fields(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    project_id = client.post(
        "/projects",
        json={"name": "Quick capture extras"},
        headers=headers,
    ).json()["data"]["project_id"]

    response = client.post(
        "/notes/quick-capture",
        data={
            "project_id": project_id,
            "status": "committed",
            "transcribed_text": "should be ignored",
            "metadata": json.dumps({"source": "camera"}),
        },
        files={"file": ("snap.jpg", b"jpeg-bytes", "image/jpeg")},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    payload = response.json()["data"]
    assert payload["status"] == "staged"
    assert payload["transcribed_text"] is None
    assert payload["metadata"]["source"] == "camera"
    _assert_source_file_metadata(
        payload["metadata"],
        filename="snap.jpg",
        content_type="image/jpeg",
        size_bytes=len(b"jpeg-bytes"),
    )
