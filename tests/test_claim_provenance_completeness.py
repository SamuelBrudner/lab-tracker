"""Claim/record-export provenance completeness (review findings M40, M41)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from lab_tracker.models import (
    Analysis,
    AnalysisStatus,
    Claim,
    ClaimStatus,
    Dataset,
    DatasetCommitManifest,
    DatasetStatus,
    RecordExportRecords,
    SupervisionEdge,
    Visualization,
)
from lab_tracker.provenance import (
    build_claim_provenance_document,
    build_dataset_provenance_document,
    build_record_export_provenance_document,
)

BASE = "http://example.test"
CREATOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-000000000001")
SUPERVISOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-000000000002")
CLAIM_SUPERVISOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-000000000003")
PROJECT = UUID("99999999-9999-9999-9999-000000000001")


def _nodes(document: dict[str, object]) -> dict[str, dict[str, object]]:
    graph = document["@graph"]
    assert isinstance(graph, list)
    return {str(node["@id"]): node for node in graph}


def _agent(user_id: UUID) -> str:
    return f"{BASE}/agents/{user_id}"


def _supervisor_ids(node: dict[str, object]) -> set[str]:
    value = node.get("actedOnBehalfOf")
    if value is None:
        return set()
    relationships = value if isinstance(value, list) else [value]
    return {str(item["@id"]) for item in relationships}


def _dataset() -> Dataset:
    return Dataset(
        dataset_id=UUID("11111111-1111-1111-1111-000000000001"),
        project_id=PROJECT,
        commit_hash="commit-supervised",
        primary_question_id=uuid4(),
        question_links=[],
        commit_manifest=DatasetCommitManifest(),
        status=DatasetStatus.COMMITTED,
        created_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        created_by_user_id=CREATOR,
    )


def _claim(dataset: Dataset, *, analysis_ids: list[UUID] | None = None) -> Claim:
    return Claim(
        claim_id=UUID("33333333-3333-3333-3333-000000000001"),
        project_id=PROJECT,
        statement="Supervised result holds.",
        confidence=0.8,
        status=ClaimStatus.SUPPORTED,
        supported_by_dataset_ids=[dataset.dataset_id],
        supported_by_analysis_ids=analysis_ids or [],
        created_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        created_by_user_id=CREATOR,
    )


def _edges() -> list[SupervisionEdge]:
    return [
        # Active when the dataset was committed, over before the claim.
        SupervisionEdge(
            edge_id=UUID("55555555-5555-5555-5555-000000000001"),
            supervisor_user_id=SUPERVISOR,
            supervisee_user_id=CREATOR,
            started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ended_at=datetime(2025, 12, 31, tzinfo=timezone.utc),
        ),
        # Active only when the claim was recorded.
        SupervisionEdge(
            edge_id=UUID("55555555-5555-5555-5555-000000000002"),
            supervisor_user_id=CLAIM_SUPERVISOR,
            supervisee_user_id=CREATOR,
            started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    ]


def test_claim_document_keeps_dataset_time_supervision() -> None:
    dataset = _dataset()
    edges = _edges()
    dataset_document = build_dataset_provenance_document(BASE, dataset, supervision_edges=edges)
    assert _supervisor_ids(_nodes(dataset_document)[_agent(CREATOR)]) == {_agent(SUPERVISOR)}

    document = build_claim_provenance_document(
        BASE,
        _claim(dataset),
        analyses=[],
        datasets=[dataset],
        questions=[],
        visualizations=[],
        supervision_edges=edges,
    )

    creator = _nodes(document)[_agent(CREATOR)]
    assert _supervisor_ids(creator) == {_agent(SUPERVISOR), _agent(CLAIM_SUPERVISOR)}


def test_record_export_document_keeps_dataset_time_supervision() -> None:
    dataset = _dataset()
    document = build_record_export_provenance_document(
        BASE,
        RecordExportRecords(datasets=[dataset], claims=[_claim(dataset)]),
        supervision_edges=_edges(),
    )

    creator = _nodes(document)[_agent(CREATOR)]
    assert _supervisor_ids(creator) == {_agent(SUPERVISOR), _agent(CLAIM_SUPERVISOR)}


def _analysis(analysis_id: UUID, dataset: Dataset) -> Analysis:
    return Analysis(
        analysis_id=analysis_id,
        project_id=PROJECT,
        dataset_ids=[dataset.dataset_id],
        method_hash="method-follow-up",
        code_version="v1",
        executed_by_user_id=CREATOR,
        executed_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        status=AnalysisStatus.COMMITTED,
    )


def test_claim_document_includes_visualizations_from_non_supporting_analyses() -> None:
    dataset = _dataset()
    follow_up = _analysis(UUID("44444444-4444-4444-4444-000000000001"), dataset)
    claim = _claim(dataset)
    visualization = Visualization(
        viz_id=UUID("66666666-6666-6666-6666-000000000001"),
        analysis_id=follow_up.analysis_id,
        viz_type="line",
        file_path="figs/follow-up.png",
        related_claim_ids=[claim.claim_id],
        created_at=datetime(2026, 3, 2, tzinfo=timezone.utc),
        created_by_user_id=CREATOR,
    )

    document = build_claim_provenance_document(
        BASE,
        claim,
        analyses=[follow_up],
        datasets=[dataset],
        questions=[],
        visualizations=[visualization],
    )

    nodes = _nodes(document)
    viz_node = nodes[f"{BASE}/visualizations/{visualization.viz_id}"]
    analysis_iri = f"{BASE}/analyses/{follow_up.analysis_id}"
    assert viz_node["wasGeneratedBy"] == {"@id": analysis_iri}
    assert analysis_iri in nodes
    claim_node = nodes[f"{BASE}/claims/{claim.claim_id}"]
    # The generating analysis is context, not support for the claim.
    assert "supportsAnalysis" not in claim_node


def test_claim_document_refuses_visualization_without_its_analysis() -> None:
    dataset = _dataset()
    claim = _claim(dataset)
    visualization = Visualization(
        viz_id=UUID("66666666-6666-6666-6666-000000000002"),
        analysis_id=UUID("44444444-4444-4444-4444-000000000009"),
        viz_type="line",
        file_path="figs/orphan.png",
        related_claim_ids=[claim.claim_id],
    )

    with pytest.raises(ValueError, match="was not provided"):
        build_claim_provenance_document(
            BASE,
            claim,
            analyses=[],
            datasets=[dataset],
            questions=[],
            visualizations=[visualization],
        )


def test_claim_provenance_route_includes_related_visualization_of_other_analysis(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    headers = admin_auth_headers
    project_id = client.post(
        "/projects", json={"name": "Viz provenance project"}, headers=headers
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does the follow-up figure reach the claim sidecar?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    dataset_response = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "status": "committed",
            "commit_manifest": {
                "files": [{"path": "raw/data.csv", "checksum": "abc123", "size_bytes": 12}]
            },
        },
        headers=headers,
    )
    assert dataset_response.status_code == 201, dataset_response.text
    dataset_id = dataset_response.json()["data"]["dataset_id"]
    claim_response = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "Dataset-supported claim",
            "confidence": 80,
            "status": "supported",
            "supported_by_dataset_ids": [dataset_id],
        },
        headers=headers,
    )
    assert claim_response.status_code == 201, claim_response.text
    claim_id = claim_response.json()["data"]["claim_id"]
    analysis_response = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "method-follow-up",
            "code_version": "git:follow-up",
            "status": "committed",
        },
        headers=headers,
    )
    assert analysis_response.status_code == 201, analysis_response.text
    analysis_id = analysis_response.json()["data"]["analysis_id"]
    viz_response = client.post(
        "/visualizations",
        json={
            "analysis_id": analysis_id,
            "viz_type": "figure",
            "file_path": "figs/follow-up.png",
            "related_claim_ids": [claim_id],
        },
        headers=headers,
    )
    assert viz_response.status_code == 201, viz_response.text
    viz_id = viz_response.json()["data"]["viz_id"]

    response = client.get(f"/claims/{claim_id}/provenance", headers=headers)

    assert response.status_code == 200, response.text
    nodes = _nodes(response.json())
    viz_node = nodes[f"http://testserver/visualizations/{viz_id}"]
    analysis_iri = f"http://testserver/analyses/{analysis_id}"
    assert viz_node["wasGeneratedBy"] == {"@id": analysis_iri}
    assert nodes[analysis_iri]["codeVersion"] == "git:follow-up"
