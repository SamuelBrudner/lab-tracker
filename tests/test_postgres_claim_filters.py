"""PostgreSQL coverage for claim support-link filters and dataset deletion."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.postgres


def _create_project_question_dataset(
    client: TestClient,
    headers: dict[str, str],
    *,
    label: str,
) -> tuple[UUID, UUID, UUID]:
    project_response = client.post(
        "/projects",
        json={"name": f"{label} project"},
        headers=headers,
    )
    assert project_response.status_code == 201, project_response.text
    project_id = UUID(project_response.json()["data"]["project_id"])
    question_response = client.post(
        "/questions",
        json={
            "project_id": str(project_id),
            "text": f"{label} question",
            "question_type": "descriptive",
        },
        headers=headers,
    )
    assert question_response.status_code == 201, question_response.text
    question_id = UUID(question_response.json()["data"]["question_id"])
    dataset_response = client.post(
        "/datasets",
        json={
            "project_id": str(project_id),
            "primary_question_id": str(question_id),
        },
        headers=headers,
    )
    assert dataset_response.status_code == 201, dataset_response.text
    dataset_id = UUID(dataset_response.json()["data"]["dataset_id"])
    return project_id, question_id, dataset_id


def _claim_ids(response) -> list[str]:  # noqa: ANN001
    assert response.status_code == 200, response.text
    return [item["claim_id"] for item in response.json()["data"]]


def _create_dataset(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: UUID,
    question_id: UUID,
) -> UUID:
    response = client.post(
        "/datasets",
        json={
            "project_id": str(project_id),
            "primary_question_id": str(question_id),
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["data"]["dataset_id"])


def _create_analysis(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: UUID,
    dataset_id: UUID,
    label: str,
) -> UUID:
    response = client.post(
        "/analyses",
        json={
            "project_id": str(project_id),
            "dataset_ids": [str(dataset_id)],
            "method_hash": f"{label}-method",
            "code_version": f"{label}-code",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["data"]["analysis_id"])


def _create_claim(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: UUID,
    dataset_id: UUID,
    analysis_id: UUID,
    label: str,
    with_citation: bool = False,
) -> str:
    payload: dict[str, object] = {
        "project_id": str(project_id),
        "statement": f"{label} remains filterable on PostgreSQL.",
        "confidence": 0.8,
        "supported_by_dataset_ids": [str(dataset_id)],
        "supported_by_analysis_ids": [str(analysis_id)],
    }
    if with_citation:
        payload["external_citations"] = [
            {
                "source_system": "doi",
                "uri": "doi:10.1101/postgres-filter",
                "content_hash": "sha256:postgres-filter",
                "metadata": {"backend": "postgresql"},
            }
        ]
    response = client.post(
        "/claims",
        json=payload,
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["claim_id"])


def test_postgres_claim_support_filters_do_not_compare_json_columns(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
) -> None:
    project_id, question_id, dataset_id = _create_project_question_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        label="Claim support filter",
    )
    other_dataset_id = _create_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        question_id=question_id,
    )
    analysis_id = _create_analysis(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        dataset_id=dataset_id,
        label="primary-filter",
    )
    other_analysis_id = _create_analysis(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        dataset_id=other_dataset_id,
        label="other-filter",
    )
    intersecting_claim_ids = [
        _create_claim(
            postgres_client,
            postgres_admin_auth_headers,
            project_id=project_id,
            dataset_id=dataset_id,
            analysis_id=analysis_id,
            label=label,
            with_citation=index == 0,
        )
        for index, label in enumerate(("First JSON-bearing claim", "Second matching claim"))
    ]
    dataset_only_claim_id = _create_claim(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        dataset_id=dataset_id,
        analysis_id=other_analysis_id,
        label="Dataset-only match",
    )
    analysis_only_claim_id = _create_claim(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        dataset_id=other_dataset_id,
        analysis_id=analysis_id,
        label="Analysis-only match",
    )

    by_dataset = postgres_client.get(
        "/claims",
        params={"dataset_id": str(dataset_id)},
        headers=postgres_admin_auth_headers,
    )
    by_analysis = postgres_client.get(
        "/claims",
        params={"analysis_id": analysis_id},
        headers=postgres_admin_auth_headers,
    )
    by_both = postgres_client.get(
        "/claims",
        params={"dataset_id": str(dataset_id), "analysis_id": str(analysis_id)},
        headers=postgres_admin_auth_headers,
    )
    paged_intersection = postgres_client.get(
        "/claims",
        params={
            "dataset_id": str(dataset_id),
            "analysis_id": str(analysis_id),
            "limit": 1,
            "offset": 1,
        },
        headers=postgres_admin_auth_headers,
    )
    missing_dataset = postgres_client.get(
        "/claims",
        params={"dataset_id": str(uuid4())},
        headers=postgres_admin_auth_headers,
    )
    mismatched_intersection = postgres_client.get(
        "/claims",
        params={"dataset_id": str(dataset_id), "analysis_id": str(uuid4())},
        headers=postgres_admin_auth_headers,
    )
    reverse_mismatch = postgres_client.get(
        "/claims",
        params={"dataset_id": str(uuid4()), "analysis_id": str(analysis_id)},
        headers=postgres_admin_auth_headers,
    )

    dataset_ids = _claim_ids(by_dataset)
    analysis_ids = _claim_ids(by_analysis)
    intersection_ids = _claim_ids(by_both)
    assert set(dataset_ids) == {*intersecting_claim_ids, dataset_only_claim_id}
    assert by_dataset.json()["meta"]["total"] == 3
    assert set(analysis_ids) == {*intersecting_claim_ids, analysis_only_claim_id}
    assert by_analysis.json()["meta"]["total"] == 3
    assert intersection_ids == intersecting_claim_ids
    assert by_both.json()["meta"]["total"] == 2
    assert _claim_ids(paged_intersection) == intersection_ids[1:2]
    assert paged_intersection.json()["meta"]["total"] == 2
    assert _claim_ids(missing_dataset) == []
    assert missing_dataset.json()["meta"]["total"] == 0
    assert _claim_ids(mismatched_intersection) == []
    assert mismatched_intersection.json()["meta"]["total"] == 0
    assert _claim_ids(reverse_mismatch) == []
    assert reverse_mismatch.json()["meta"]["total"] == 0


def test_postgres_dataset_delete_distinguishes_unreferenced_and_claimed_datasets(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
) -> None:
    project_id, question_id, claimed_dataset_id = _create_project_question_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        label="Dataset claim guard",
    )
    unreferenced_response = postgres_client.post(
        "/datasets",
        json={
            "project_id": str(project_id),
            "primary_question_id": str(question_id),
        },
        headers=postgres_admin_auth_headers,
    )
    assert unreferenced_response.status_code == 201, unreferenced_response.text
    unreferenced_dataset_id = unreferenced_response.json()["data"]["dataset_id"]
    claim_response = postgres_client.post(
        "/claims",
        json={
            "project_id": str(project_id),
            "statement": "This staged dataset remains referenced.",
            "confidence": 0.5,
            "supported_by_dataset_ids": [str(claimed_dataset_id)],
        },
        headers=postgres_admin_auth_headers,
    )
    assert claim_response.status_code == 201, claim_response.text

    unreferenced_delete = postgres_client.delete(
        f"/datasets/{unreferenced_dataset_id}",
        headers=postgres_admin_auth_headers,
    )
    claimed_delete = postgres_client.delete(
        f"/datasets/{claimed_dataset_id}",
        headers=postgres_admin_auth_headers,
    )

    assert unreferenced_delete.status_code == 200, unreferenced_delete.text
    assert claimed_delete.status_code == 422, claimed_delete.text
    assert claimed_delete.json()["error"]["code"] == "validation_error"
    assert "claims reference it" in claimed_delete.json()["error"]["message"]
