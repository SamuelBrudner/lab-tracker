from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import update

from lab_tracker.db_models import ClaimModel, DatasetModel


def _create_project(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _create_question(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    *,
    text: str = "Does the readiness check pass?",
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


def _create_committed_dataset(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    question_id: str,
    *,
    external_artifact_uri: str = "s3://lab-bucket/acquisitions/run-001/manifest.json",
) -> str:
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
                        "uri": external_artifact_uri,
                        "content_hash": "sha256:manifest001",
                    }
                ]
            },
        },
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["dataset_id"]


def test_publication_readiness_flags_structural_failures(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Blocked ARA project")
    answered_question_id = _create_question(
        client,
        admin_auth_headers,
        project_id,
        text="Answered without any committed dataset?",
        status="answered",
    )
    grounded_question_id = _create_question(
        client,
        admin_auth_headers,
        project_id,
        text="Grounded question",
    )
    dataset_id = _create_committed_dataset(
        client,
        admin_auth_headers,
        project_id,
        grounded_question_id,
    )
    claim_id = str(uuid4())
    with client.app.state.db_session_factory() as session:
        session.execute(
            update(DatasetModel)
            .where(DatasetModel.dataset_id == dataset_id)
            .values(
                manifest_external_artifacts=[
                    {
                        "kind": "entity",
                        "source_system": "s3",
                        "uri": "not a valid iri",
                        "content_hash": "sha256:manifest001",
                    }
                ]
            )
        )
        session.add(
            ClaimModel(
                claim_id=claim_id,
                project_id=project_id,
                statement="Legacy supported claim with no evidence.",
                confidence=0.8,
                status="supported",
            )
        )
        session.commit()

    response = client.get(
        f"/projects/{project_id}/publication-readiness",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    report = response.json()["data"]
    assert report["seal_level"] == "blocked"
    assert [
        (item["claim_id"], item["reason"]) for item in report["unsupported_claims"]
    ] == [
        (claim_id, "Supported claim has no dataset or analysis evidence."),
        (claim_id, "Supported claim has no falsification criteria."),
    ]
    assert [item["question_id"] for item in report["ungrounded_questions"]] == [
        answered_question_id
    ]
    assert report["orphaned_entities"] == []
    assert report["broken_external_refs"] == [
        {
            "entity_type": "dataset",
            "entity_id": dataset_id,
            "source_system": "s3",
            "uri": "not a valid iri",
            "reason": "External artifact URI must not contain spaces or control characters.",
        }
    ]


def test_publication_readiness_passes_clean_project(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Clean ARA project")
    question_id = _create_question(
        client,
        admin_auth_headers,
        project_id,
        status="active",
    )
    dataset_id = _create_committed_dataset(
        client,
        admin_auth_headers,
        project_id,
        question_id,
    )
    question_response = client.patch(
        f"/questions/{question_id}",
        json={"status": "answered"},
        headers=admin_auth_headers,
    )
    assert question_response.status_code == 200
    claim_response = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "The clean project is grounded.",
            "confidence": 0.9,
            "status": "supported",
            "falsification_criteria": "A repeat committed dataset refutes the effect.",
            "supported_by_dataset_ids": [dataset_id],
        },
        headers=admin_auth_headers,
    )
    assert claim_response.status_code == 201

    response = client.get(
        f"/projects/{project_id}/publication-readiness",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    report = response.json()["data"]
    assert report["seal_level"] == "ara_l1"
    assert report["unsupported_claims"] == []
    assert report["ungrounded_questions"] == []
    assert report["orphaned_entities"] == []
    assert report["broken_external_refs"] == []


def test_external_artifact_uri_is_validated_on_write(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Malformed refs")
    question_id = _create_question(client, admin_auth_headers, project_id)
    dataset_response = client.post(
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
                        "uri": "not a valid iri",
                        "content_hash": "sha256:manifest001",
                    }
                ]
            },
        },
        headers=admin_auth_headers,
    )
    assert dataset_response.status_code == 422
    assert "well-formed IRI" in dataset_response.json()["error"]["message"] or (
        "spaces or control characters" in dataset_response.json()["error"]["message"]
    )

    dataset_id = _create_committed_dataset(
        client,
        admin_auth_headers,
        project_id,
        question_id,
    )
    analysis_response = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "method-1",
            "code_version": "git:abc123",
            "external_artifacts": [
                {
                    "kind": "activity",
                    "source_system": "mlflow",
                    "uri": "not a valid iri",
                    "content_hash": "sha256:run001",
                }
            ],
        },
        headers=admin_auth_headers,
    )
    assert analysis_response.status_code == 422
    assert "spaces or control characters" in analysis_response.json()["error"]["message"]
