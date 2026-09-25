from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import update

from lab_tracker.db_models import ClaimModel, DatasetModel
from lab_tracker.models import Claim, ClaimStatus
from lab_tracker.services.publication_readiness_service import (
    STALE_TESTING_CLAIM_DAYS,
    _stale_predictions,
)


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
    )
    # A question is answered by moving it from active; it cannot be created answered.
    answered = client.patch(
        f"/questions/{answered_question_id}",
        json={"status": "answered"},
        headers=admin_auth_headers,
    )
    assert answered.status_code == 200
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
    assert report["contested_claims"] == []
    assert report["stale_predictions"] == []
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


def _supported_claim(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    dataset_id: str,
    statement: str,
) -> str:
    response = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": statement,
            "confidence": 80,
            "status": "supported",
            "falsification_criteria": "A repeat dataset shows no effect.",
            "supported_by_dataset_ids": [dataset_id],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["claim_id"]


def _readiness(client: TestClient, headers: dict[str, str], project_id: str) -> dict:
    response = client.get(f"/projects/{project_id}/publication-readiness", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_publication_readiness_flags_superseded_or_invalidated_supported_claims(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Contested ARA project")
    question_id = _create_question(client, admin_auth_headers, project_id)
    dataset_id = _create_committed_dataset(client, admin_auth_headers, project_id, question_id)
    claim_a = _supported_claim(client, admin_auth_headers, project_id, dataset_id, "Claim A")
    claim_b = _supported_claim(client, admin_auth_headers, project_id, dataset_id, "Claim B")
    edge = client.post(
        f"/claims/{claim_b}/edges",
        json={"target_claim_id": claim_a, "relation": "supersedes"},
        headers=admin_auth_headers,
    )
    assert edge.status_code == 201, edge.text

    superseded = _readiness(client, admin_auth_headers, project_id)
    assert superseded["seal_level"] == "blocked"
    assert superseded["unsupported_claims"] == []
    assert superseded["contested_claims"] == [
        {
            "claim_id": claim_a,
            "statement": "Claim A",
            "status": "supported",
            "effective_status": "superseded",
            "reason": f"Supported claim is superseded by claim {claim_b}.",
        }
    ]

    removed = client.delete(
        f"/claims/{claim_b}/edges/{edge.json()['data']['edge_id']}",
        headers=admin_auth_headers,
    )
    assert removed.status_code == 200, removed.text
    clean = _readiness(client, admin_auth_headers, project_id)
    assert clean["seal_level"] == "ara_l1"
    assert clean["contested_claims"] == []

    refute = client.post(
        f"/claims/{claim_b}/edges",
        json={"target_claim_id": claim_a, "relation": "refutes"},
        headers=admin_auth_headers,
    )
    assert refute.status_code == 201, refute.text
    contested = _readiness(client, admin_auth_headers, project_id)
    assert contested["seal_level"] == "blocked"
    assert [item["effective_status"] for item in contested["contested_claims"]] == ["contested"]
    assert contested["contested_claims"][0]["reason"] == (
        f"Supported claim is contested by claim(s) {claim_b}."
    )

    pivot = client.post(
        "/exploration-nodes",
        json={
            "project_id": project_id,
            "node_type": "pivot",
            "title": "Drop claim A",
            "target": {"entity_type": "claim", "entity_id": claim_a},
            "status": "committed",
            "trigger": "The replication failed.",
            "rationale": "The effect did not hold.",
            "invalidates_claim_id": claim_a,
        },
        headers=admin_auth_headers,
    )
    assert pivot.status_code == 201, pivot.text
    invalidated = _readiness(client, admin_auth_headers, project_id)
    assert invalidated["seal_level"] == "blocked"
    [row] = invalidated["contested_claims"]
    assert row["effective_status"] == "invalidated"
    assert row["reason"] == (
        "Supported claim is invalidated by exploration node "
        f"{pivot.json()['data']['node_id']}."
    )


def _rewind_created_at(client: TestClient, claim_id: str, *, days: int) -> None:
    with client.app.state.db_session_factory() as session:
        session.execute(
            update(ClaimModel)
            .where(ClaimModel.claim_id == claim_id)
            .values(created_at=datetime.now(timezone.utc) - timedelta(days=days))
        )
        session.commit()


def test_stale_testing_prediction_is_reported_without_blocking(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Stale predictions")
    question_id = _create_question(client, admin_auth_headers, project_id)
    _create_committed_dataset(client, admin_auth_headers, project_id, question_id)
    other_question_id = _create_question(
        client, admin_auth_headers, project_id, text="A question with no data yet"
    )

    def testing_claim(statement: str, question: str) -> str:
        created = client.post(
            "/claims",
            json={
                "project_id": project_id,
                "statement": statement,
                "confidence": 50,
                "answers_question_ids": [question],
            },
            headers=admin_auth_headers,
        )
        assert created.status_code == 201, created.text
        claim_id = created.json()["data"]["claim_id"]
        moved = client.patch(
            f"/claims/{claim_id}", json={"status": "testing"}, headers=admin_auth_headers
        )
        assert moved.status_code == 200, moved.text
        return claim_id

    stale_id = testing_claim("Old prediction with data", question_id)
    fresh_id = testing_claim("Recent prediction", question_id)
    ungrounded_id = testing_claim("Old prediction without data", other_question_id)
    _rewind_created_at(client, stale_id, days=STALE_TESTING_CLAIM_DAYS + 1)
    _rewind_created_at(client, fresh_id, days=5)
    _rewind_created_at(client, ungrounded_id, days=STALE_TESTING_CLAIM_DAYS + 1)

    report = _readiness(client, admin_auth_headers, project_id)

    assert report["seal_level"] == "ara_l1", "stale predictions are advisory"
    assert report["contested_claims"] == []
    [stale] = report["stale_predictions"]
    assert stale["claim_id"] == stale_id
    assert stale["status"] == "testing"
    assert stale["question_ids"] == [question_id]
    assert stale["age_days"] >= STALE_TESTING_CLAIM_DAYS
    assert stale["reason"] == (
        f"Testing claim is {stale['age_days']} days old and its question already has "
        "committed data; resolve it to supported or rejected."
    )


def test_stale_predictions_pure_function_uses_injected_now() -> None:
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    question_id = uuid4()

    def claim(status: ClaimStatus, age_days: int, question: UUID = question_id) -> Claim:
        return Claim(
            claim_id=uuid4(),
            project_id=uuid4(),
            statement=f"{status.value} {age_days}",
            confidence=50,
            status=status,
            answers_question_ids=[question],
            created_at=now - timedelta(days=age_days),
        )

    boundary = claim(ClaimStatus.TESTING, STALE_TESTING_CLAIM_DAYS)
    just_under = claim(ClaimStatus.TESTING, STALE_TESTING_CLAIM_DAYS - 1)
    proposed = claim(ClaimStatus.PROPOSED, STALE_TESTING_CLAIM_DAYS + 10)
    ungrounded = claim(ClaimStatus.TESTING, STALE_TESTING_CLAIM_DAYS + 10, uuid4())

    stale = _stale_predictions(
        [boundary, just_under, proposed, ungrounded],
        committed_question_ids={question_id},
        now=now,
    )

    assert [item.claim_id for item in stale] == [boundary.claim_id]
    assert stale[0].age_days == STALE_TESTING_CLAIM_DAYS
    assert stale[0].question_ids == [question_id]
