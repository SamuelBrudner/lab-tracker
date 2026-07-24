from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from lab_tracker.api import LabTrackerAPI
from lab_tracker.application.context_queries import ContextQueries
from lab_tracker.artifact_resolution import (
    LocalFilesystemResolver,
    ResolverRegistry,
)
from lab_tracker.auth import utc_now
from lab_tracker.errors import NotFoundError
from lab_tracker.models import (
    EntityRef,
    EntityType,
    ProvenanceLink,
    ProvenanceLinkBasis,
    ProvenanceLinkOrigin,
    ProvenanceLinkRelation,
    ProvenanceLinkStatus,
)
from lab_tracker.services.analysis_service import AnalysisService
from lab_tracker.services.dataset_service import DatasetService
from lab_tracker.services.question_service import QuestionService
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


@dataclass(frozen=True)
class EvidenceArtifactRecords:
    project_id: str
    question_id: str
    dataset_id: str
    dataset_file_id: str
    analysis_id: str
    claim_id: str
    target_claim_id: str
    visualization_id: str
    visualization_without_asset_id: str
    exploration_node_id: str
    provenance_link_id: str
    artifact_content: bytes
    artifact_hash: str
    dataset_file_content: bytes
    visualization_content: bytes
    missing_dataset_id: str
    missing_dataset_file_id: str
    missing_analysis_id: str
    missing_claim_id: str
    missing_visualization_id: str
    missing_exploration_node_id: str
    missing_provenance_link_id: str


@dataclass(frozen=True)
class ReadCase:
    name: str
    existing_path: str
    missing_path: str
    not_found_label: str
    media_type: str
    authorized_value: Callable[[Any], object]
    expected_value: object
    accept: str = "application/json"


@dataclass(frozen=True)
class ResolverCase:
    entity_type: str
    existing_id: str
    missing_id: str
    not_found_label: str


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _created_id(response, field: str) -> str:  # noqa: ANN001
    assert response.status_code == 201, response.text
    return response.json()["data"][field]


def _graph_contains(response, iri: str) -> bool:  # noqa: ANN001
    graph = response.json()["@graph"]
    return any(node.get("@id") == iri for node in graph)


def _download_contract(response) -> tuple[bytes, str, str, str]:  # noqa: ANN001
    return (
        response.content,
        response.headers["content-disposition"],
        response.headers["content-length"],
        response.headers["content-type"].split(";", maxsplit=1)[0],
    )


def _visualization_download_contract(
    response,
) -> tuple[bytes, str, str, str, str]:  # noqa: ANN001
    return (*_download_contract(response), response.headers["x-content-type-options"])


def _create_evidence_artifact_records(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    artifact_root: Path,
) -> EvidenceArtifactRecords:
    artifact_content = b"opaque external artifact bytes"
    artifact = artifact_root / f"artifact-{uuid4().hex}.bin"
    artifact.write_bytes(artifact_content)
    artifact_hash = _sha256(artifact_content)
    artifact_reference = {
        "source_system": "local",
        "uri": artifact.as_uri(),
        "content_hash": artifact_hash,
    }
    client.app.state.resolver_registry = ResolverRegistry(
        [LocalFilesystemResolver(allowed_roots=[artifact_root])]
    )

    question_id = _created_id(
        client.post(
            "/questions",
            json={
                "project_id": project_id,
                "text": "Are evidence and artifact reads project-opaque?",
                "question_type": "descriptive",
                "status": "active",
            },
            headers=headers,
        ),
        "question_id",
    )
    dataset_id = _created_id(
        client.post(
            "/datasets",
            json={
                "project_id": project_id,
                "primary_question_id": question_id,
                "status": "staged",
                "commit_manifest": {
                    "external_artifacts": [artifact_reference],
                    "metadata": {"opacity_contract": "true"},
                },
            },
            headers=headers,
        ),
        "dataset_id",
    )

    dataset_file_content = b"opaque attached dataset bytes"
    dataset_file_response = client.post(
        f"/datasets/{dataset_id}/files",
        files={
            "file": (
                "opaque.txt",
                dataset_file_content,
                "text/plain",
            )
        },
        headers=headers,
    )
    assert dataset_file_response.status_code == 201, dataset_file_response.text
    dataset_file_id = dataset_file_response.json()["data"]["file_id"]

    analysis_id = _created_id(
        client.post(
            "/analyses",
            json={
                "project_id": project_id,
                "dataset_ids": [dataset_id],
                "method_hash": "opacity-method",
                "code_version": "git:opacity",
                "external_artifacts": [artifact_reference],
            },
            headers=headers,
        ),
        "analysis_id",
    )
    claim_id = _created_id(
        client.post(
            "/claims",
            json={
                "project_id": project_id,
                "statement": "Opaque reads do not disclose record existence.",
                "confidence": 0.8,
                "supported_by_dataset_ids": [dataset_id],
                "supported_by_analysis_ids": [analysis_id],
                "answers_question_ids": [question_id],
                "external_citations": [artifact_reference],
            },
            headers=headers,
        ),
        "claim_id",
    )
    updated_claim = client.patch(
        f"/claims/{claim_id}",
        json={"statement": "Opaque reads conceal existence and child state."},
        headers=headers,
    )
    assert updated_claim.status_code == 200, updated_claim.text

    target_claim_id = _created_id(
        client.post(
            "/claims",
            json={
                "project_id": project_id,
                "statement": "A second claim gives the edge route a child.",
                "confidence": 0.7,
            },
            headers=headers,
        ),
        "claim_id",
    )
    edge = client.post(
        f"/claims/{claim_id}/edges",
        json={"target_claim_id": target_claim_id, "relation": "extends"},
        headers=headers,
    )
    assert edge.status_code == 201, edge.text

    visualization_id = _created_id(
        client.post(
            "/visualizations",
            json={
                "analysis_id": analysis_id,
                "viz_type": "figure",
                "file_path": "figures/opaque.png",
                "caption": "Opaque managed visualization",
                "related_claim_ids": [claim_id],
            },
            headers=headers,
        ),
        "viz_id",
    )
    visualization_content = b"\x89PNG\r\n\x1a\nopaque-visualization"
    visualization_upload = client.post(
        f"/visualizations/{visualization_id}/file",
        files={
            "file": (
                "opaque.png",
                visualization_content,
                "image/png",
            )
        },
        headers=headers,
    )
    assert visualization_upload.status_code == 201, visualization_upload.text

    visualization_without_asset_id = _created_id(
        client.post(
            "/visualizations",
            json={
                "analysis_id": analysis_id,
                "viz_type": "figure",
                "file_path": "figures/no-asset.png",
                "caption": "Authorized child absence",
            },
            headers=headers,
        ),
        "viz_id",
    )

    exploration_node_id = _created_id(
        client.post(
            "/exploration-nodes",
            json={
                "project_id": project_id,
                "node_type": "decision",
                "title": "Keep evidence reads opaque",
                "choice": "Authorize the parent before traversing children.",
                "alternatives_considered": [
                    "Expose authorization errors after loading the target."
                ],
                "target": {
                    "entity_type": "claim",
                    "entity_id": claim_id,
                },
                "rationale": "Existence is project-confidential.",
            },
            headers=headers,
        ),
        "node_id",
    )

    provenance_link_id = uuid4()
    with client.app.state.db_session_factory() as session:
        repository = SQLAlchemyLabTrackerRepository(session)
        repository.provenance_links.save(
            ProvenanceLink(
                link_id=provenance_link_id,
                project_id=UUID(project_id),
                source=EntityRef(
                    entity_type=EntityType.CLAIM,
                    entity_id=UUID(claim_id),
                ),
                target=EntityRef(
                    entity_type=EntityType.CLAIM,
                    entity_id=UUID(target_claim_id),
                ),
                relation=ProvenanceLinkRelation.WAS_DERIVED_FROM,
                basis=ProvenanceLinkBasis.CONTENT_HASH_MATCH,
                content_hash=artifact_hash,
                status=ProvenanceLinkStatus.PROPOSED,
                origin=ProvenanceLinkOrigin.SYSTEM_DETECTED,
            )
        )
        repository.commit()

    return EvidenceArtifactRecords(
        project_id=project_id,
        question_id=question_id,
        dataset_id=dataset_id,
        dataset_file_id=dataset_file_id,
        analysis_id=analysis_id,
        claim_id=claim_id,
        target_claim_id=target_claim_id,
        visualization_id=visualization_id,
        visualization_without_asset_id=visualization_without_asset_id,
        exploration_node_id=exploration_node_id,
        provenance_link_id=str(provenance_link_id),
        artifact_content=artifact_content,
        artifact_hash=artifact_hash,
        dataset_file_content=dataset_file_content,
        visualization_content=visualization_content,
        missing_dataset_id=str(uuid4()),
        missing_dataset_file_id=str(uuid4()),
        missing_analysis_id=str(uuid4()),
        missing_claim_id=str(uuid4()),
        missing_visualization_id=str(uuid4()),
        missing_exploration_node_id=str(uuid4()),
        missing_provenance_link_id=str(uuid4()),
    )


@pytest.fixture()
def evidence_artifact_records(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
    tmp_path: Path,
) -> EvidenceArtifactRecords:
    return _create_evidence_artifact_records(
        client,
        admin_auth_headers,
        project_id=scoped_project_member.hidden_project_id,
        artifact_root=tmp_path,
    )


def _read_cases(
    records: EvidenceArtifactRecords,
) -> dict[str, tuple[ReadCase, ...]]:
    base_url = "http://testserver"
    return {
        "datasets": (
            ReadCase(
                "dataset-detail",
                f"/datasets/{records.dataset_id}",
                f"/datasets/{records.missing_dataset_id}",
                "Dataset",
                "application/json",
                lambda response: (
                    response.json()["data"]["dataset_id"],
                    response.json()["meta"]["iri"],
                ),
                (
                    records.dataset_id,
                    f"{base_url}/datasets/{records.dataset_id}",
                ),
            ),
            ReadCase(
                "dataset-files",
                f"/datasets/{records.dataset_id}/files?limit=50&offset=0",
                f"/datasets/{records.missing_dataset_id}/files?limit=50&offset=0",
                "Dataset",
                "application/json",
                lambda response: (
                    response.json()["meta"]["total"],
                    response.json()["data"][0]["file_id"],
                ),
                (1, records.dataset_file_id),
            ),
            ReadCase(
                "dataset-file-download",
                (
                    f"/datasets/{records.dataset_id}/files/"
                    f"{records.dataset_file_id}/download"
                ),
                (
                    f"/datasets/{records.missing_dataset_id}/files/"
                    f"{records.missing_dataset_file_id}/download"
                ),
                "Dataset",
                "text/plain",
                _download_contract,
                (
                    records.dataset_file_content,
                    'attachment; filename="opaque.txt"',
                    str(len(records.dataset_file_content)),
                    "text/plain",
                ),
                accept="*/*",
            ),
            ReadCase(
                "dataset-provenance",
                f"/datasets/{records.dataset_id}/provenance",
                f"/datasets/{records.missing_dataset_id}/provenance",
                "Dataset",
                "application/ld+json",
                lambda response: _graph_contains(
                    response,
                    f"{base_url}/datasets/{records.dataset_id}",
                ),
                True,
                accept="application/ld+json",
            ),
        ),
        "analyses": (
            ReadCase(
                "analysis-detail",
                f"/analyses/{records.analysis_id}",
                f"/analyses/{records.missing_analysis_id}",
                "Analysis",
                "application/json",
                lambda response: (
                    response.json()["data"]["analysis_id"],
                    response.json()["meta"]["iri"],
                ),
                (
                    records.analysis_id,
                    f"{base_url}/analyses/{records.analysis_id}",
                ),
            ),
            ReadCase(
                "analysis-provenance",
                f"/analyses/{records.analysis_id}/provenance",
                f"/analyses/{records.missing_analysis_id}/provenance",
                "Analysis",
                "application/ld+json",
                lambda response: _graph_contains(
                    response,
                    f"{base_url}/analyses/{records.analysis_id}",
                ),
                True,
                accept="application/ld+json",
            ),
        ),
        "claims": (
            ReadCase(
                "claim-detail",
                f"/claims/{records.claim_id}",
                f"/claims/{records.missing_claim_id}",
                "Claim",
                "application/json",
                lambda response: (
                    response.json()["data"]["claim_id"],
                    response.json()["meta"]["iri"],
                ),
                (
                    records.claim_id,
                    f"{base_url}/claims/{records.claim_id}",
                ),
            ),
            ReadCase(
                "claim-versions",
                f"/claims/{records.claim_id}/versions?limit=50&offset=0",
                f"/claims/{records.missing_claim_id}/versions?limit=50&offset=0",
                "Claim",
                "application/json",
                lambda response: response.json()["meta"]["total"],
                2,
            ),
            ReadCase(
                "claim-version-diff",
                (
                    f"/claims/{records.claim_id}/versions/diff"
                    "?from_version=1&to_version=2"
                ),
                (
                    f"/claims/{records.missing_claim_id}/versions/diff"
                    "?from_version=1&to_version=2"
                ),
                "Claim",
                "application/json",
                lambda response: response.json()["data"]["changed_fields"]["statement"],
                {
                    "before": "Opaque reads do not disclose record existence.",
                    "after": "Opaque reads conceal existence and child state.",
                },
            ),
            ReadCase(
                "claim-edges",
                f"/claims/{records.claim_id}/edges?limit=50&offset=0",
                f"/claims/{records.missing_claim_id}/edges?limit=50&offset=0",
                "Claim",
                "application/json",
                lambda response: (
                    response.json()["meta"]["total"],
                    response.json()["data"][0]["relation"],
                ),
                (1, "extends"),
            ),
            ReadCase(
                "claim-provenance",
                f"/claims/{records.claim_id}/provenance",
                f"/claims/{records.missing_claim_id}/provenance",
                "Claim",
                "application/ld+json",
                lambda response: _graph_contains(
                    response,
                    f"{base_url}/claims/{records.claim_id}",
                ),
                True,
                accept="application/ld+json",
            ),
        ),
        "visualizations": (
            ReadCase(
                "visualization-detail",
                f"/visualizations/{records.visualization_id}",
                f"/visualizations/{records.missing_visualization_id}",
                "Visualization",
                "application/json",
                lambda response: (
                    response.json()["data"]["viz_id"],
                    response.json()["data"]["asset"]["checksum"],
                ),
                (
                    records.visualization_id,
                    hashlib.sha256(records.visualization_content).hexdigest(),
                ),
            ),
            ReadCase(
                "visualization-file-download",
                f"/visualizations/{records.visualization_id}/file/download",
                f"/visualizations/{records.missing_visualization_id}/file/download",
                "Visualization",
                "image/png",
                _visualization_download_contract,
                (
                    records.visualization_content,
                    'attachment; filename="opaque.png"',
                    str(len(records.visualization_content)),
                    "image/png",
                    "nosniff",
                ),
                accept="*/*",
            ),
        ),
        "exploration": (
            ReadCase(
                "exploration-node-detail",
                f"/exploration-nodes/{records.exploration_node_id}",
                f"/exploration-nodes/{records.missing_exploration_node_id}",
                "Exploration node",
                "application/json",
                lambda response: response.json()["data"]["node_id"],
                records.exploration_node_id,
            ),
        ),
        "provenance-links": (
            ReadCase(
                "provenance-link-detail",
                f"/provenance-links/{records.provenance_link_id}",
                f"/provenance-links/{records.missing_provenance_link_id}",
                "Provenance link",
                "application/json",
                lambda response: response.json()["data"]["link_id"],
                records.provenance_link_id,
            ),
        ),
    }


def _resolver_cases(
    records: EvidenceArtifactRecords,
) -> tuple[ResolverCase, ...]:
    return (
        ResolverCase(
            "dataset",
            records.dataset_id,
            records.missing_dataset_id,
            "Dataset",
        ),
        ResolverCase(
            "analysis",
            records.analysis_id,
            records.missing_analysis_id,
            "Analysis",
        ),
        ResolverCase(
            "claim",
            records.claim_id,
            records.missing_claim_id,
            "Claim",
        ),
    )


def _request_headers(
    auth_headers: dict[str, str],
    case: ReadCase,
) -> dict[str, str]:
    return {**auth_headers, "Accept": case.accept}


def _not_found_body(label: str) -> dict[str, object]:
    return {
        "error": {
            "code": "not_found",
            "message": f"{label} does not exist.",
            "issues": None,
        }
    }


def _resolver_payload(
    case: ResolverCase,
    *,
    entity_id: str | None = None,
    **overrides: object,
) -> dict[str, object]:
    return {
        "entity_type": case.entity_type,
        "entity_id": entity_id or case.existing_id,
        "artifact_index": 0,
        **overrides,
    }


@pytest.mark.parametrize(
    "domain",
    (
        "datasets",
        "analyses",
        "claims",
        "visualizations",
        "exploration",
        "provenance-links",
    ),
)
def test_evidence_artifact_read_variants_are_opaque_and_preserve_contracts(
    domain: str,
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
    evidence_artifact_records: EvidenceArtifactRecords,
) -> None:
    cases_by_domain = _read_cases(evidence_artifact_records)
    assert sum(len(cases) for cases in cases_by_domain.values()) == 15

    for case in cases_by_domain[domain]:
        authorized = client.get(
            case.existing_path,
            headers=_request_headers(admin_auth_headers, case),
        )
        assert authorized.status_code == 200, f"{case.name}: {authorized.text}"
        assert authorized.headers["content-type"].startswith(case.media_type), case.name
        assert case.authorized_value(authorized) == case.expected_value, case.name

        outsider_existing = client.get(
            case.existing_path,
            headers=_request_headers(scoped_project_member.member_headers, case),
        )
        outsider_missing = client.get(
            case.missing_path,
            headers=_request_headers(scoped_project_member.member_headers, case),
        )

        assert outsider_existing.status_code == outsider_missing.status_code == 404, (
            case.name,
            outsider_existing.text,
            outsider_missing.text,
        )
        assert outsider_existing.json() == outsider_missing.json() == _not_found_body(
            case.not_found_label
        ), case.name


@pytest.mark.parametrize(
    ("entity_type", "id_attribute", "not_found_label"),
    (
        ("dataset", "dataset_id", "Dataset"),
        ("analysis", "analysis_id", "Analysis"),
        ("claim", "claim_id", "Claim"),
    ),
)
def test_jsonld_negotiated_detail_reads_use_the_same_opaque_root_boundary(
    entity_type: str,
    id_attribute: str,
    not_found_label: str,
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
    evidence_artifact_records: EvidenceArtifactRecords,
) -> None:
    entity_id = getattr(evidence_artifact_records, id_attribute)
    missing_id = getattr(evidence_artifact_records, f"missing_{id_attribute}")
    collection = "analyses" if entity_type == "analysis" else f"{entity_type}s"
    path = f"/{collection}/{entity_id}"
    missing_path = f"/{collection}/{missing_id}"
    headers = {"Accept": "application/ld+json"}

    authorized = client.get(path, headers={**admin_auth_headers, **headers})
    assert authorized.status_code == 200, authorized.text
    assert authorized.headers["content-type"].startswith("application/ld+json")
    assert _graph_contains(authorized, f"http://testserver/{collection}/{entity_id}")
    provenance = client.get(
        f"{path}/provenance",
        headers=admin_auth_headers,
    )
    assert provenance.status_code == 200, provenance.text
    assert provenance.headers["content-type"].startswith("application/ld+json")
    assert authorized.json() == provenance.json()

    outsider_existing = client.get(
        path,
        headers={**scoped_project_member.member_headers, **headers},
    )
    outsider_missing = client.get(
        missing_path,
        headers={**scoped_project_member.member_headers, **headers},
    )
    assert outsider_existing.status_code == outsider_missing.status_code == 404
    assert outsider_existing.json() == outsider_missing.json() == _not_found_body(
        not_found_label
    )


@pytest.mark.parametrize("entity_type", ("dataset", "analysis", "claim"))
def test_resolver_entity_modes_are_opaque_before_index_hash_or_resolution(
    entity_type: str,
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
    evidence_artifact_records: EvidenceArtifactRecords,
) -> None:
    case = next(
        item
        for item in _resolver_cases(evidence_artifact_records)
        if item.entity_type == entity_type
    )
    authorized = client.post(
        "/external-artifacts/resolve",
        json=_resolver_payload(case),
        headers=admin_auth_headers,
    )
    assert authorized.status_code == 200, authorized.text
    body = authorized.json()["data"]
    assert body["status"] == "verified"
    assert body["entity_type"] == entity_type
    assert body["entity_id"] == case.existing_id
    assert body["observed_hash"] == evidence_artifact_records.artifact_hash
    assert base64.b64decode(body["content_base64"]) == (
        evidence_artifact_records.artifact_content
    )

    outsider_payloads = (
        _resolver_payload(case),
        _resolver_payload(case, artifact_index=99),
        _resolver_payload(case, content_hash=_sha256(b"wrong")),
    )
    outsider_responses = [
        client.post(
            "/external-artifacts/resolve",
            json=payload,
            headers=scoped_project_member.member_headers,
        )
        for payload in outsider_payloads
    ]
    outsider_missing = client.post(
        "/external-artifacts/resolve",
        json=_resolver_payload(case, entity_id=case.missing_id),
        headers=scoped_project_member.member_headers,
    )

    for response in [*outsider_responses, outsider_missing]:
        assert response.status_code == 404, response.text
        assert response.json() == _not_found_body(case.not_found_label)


def test_all_fifteen_reads_and_three_resolver_modes_still_require_authentication(
    client: TestClient,
    evidence_artifact_records: EvidenceArtifactRecords,
) -> None:
    cases = [
        case
        for domain_cases in _read_cases(evidence_artifact_records).values()
        for case in domain_cases
    ]
    resolver_cases = _resolver_cases(evidence_artifact_records)
    assert len(cases) == 15
    assert len(resolver_cases) == 3

    responses = [
        (
            case.name,
            client.get(case.existing_path, headers={"Accept": case.accept}),
        )
        for case in cases
    ]
    responses.extend(
        (
            f"resolver-{case.entity_type}",
            client.post(
                "/external-artifacts/resolve",
                json=_resolver_payload(case),
            ),
        )
        for case in resolver_cases
    )

    for name, response in responses:
        assert response.status_code == 401, f"{name}: {response.text}"
        assert response.json()["error"] == {
            "code": "auth_error",
            "message": "Missing Authorization header.",
            "issues": None,
        }, name


def _issue_token(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    *,
    label: str,
    read_only: bool,
    scope: str,
) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={
            "label": label,
            "role": "admin",
            "read_only": read_only,
            "scope": scope,
            "expires_at": (utc_now() + timedelta(days=1)).isoformat(),
        },
        headers=admin_auth_headers,
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['data']['secret']}"}


def test_invalid_credentials_and_capability_tokens_keep_transport_semantics(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    evidence_artifact_records: EvidenceArtifactRecords,
) -> None:
    resolver_case = _resolver_cases(evidence_artifact_records)[0]
    invalid_headers = {"Authorization": "Bearer invalid-token"}
    invalid_get = client.get(
        f"/datasets/{evidence_artifact_records.dataset_id}",
        headers=invalid_headers,
    )
    invalid_resolve = client.post(
        "/external-artifacts/resolve",
        json=_resolver_payload(resolver_case),
        headers=invalid_headers,
    )
    for response in (invalid_get, invalid_resolve):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "auth_error"

    batch_headers = _issue_token(
        client,
        admin_auth_headers,
        label="Opaque batch-only capability",
        read_only=False,
        scope="batch_run_due",
    )
    forbidden_get = client.get(
        f"/datasets/{evidence_artifact_records.dataset_id}",
        headers=batch_headers,
    )
    forbidden_resolve = client.post(
        "/external-artifacts/resolve",
        json=_resolver_payload(resolver_case),
        headers=batch_headers,
    )
    for response in (forbidden_get, forbidden_resolve):
        assert response.status_code == 403
        assert response.json()["error"] == {
            "code": "service_forbidden",
            "message": "Not permitted for this token.",
            "issues": None,
        }

    read_only_headers = _issue_token(
        client,
        admin_auth_headers,
        label="Ordinary read-only capability",
        read_only=True,
        scope="all",
    )
    allowed_get = client.get(
        f"/datasets/{evidence_artifact_records.dataset_id}",
        headers=read_only_headers,
    )
    forbidden_post_read = client.post(
        "/external-artifacts/resolve",
        json=_resolver_payload(resolver_case),
        headers=read_only_headers,
    )
    assert allowed_get.status_code == 200, allowed_get.text
    # POST-based read-only resolution is deliberately retained for the
    # dedicated capability-policy follow-up; this bead must not widen LPATs.
    assert forbidden_post_read.status_code == 403, forbidden_post_read.text
    assert forbidden_post_read.json()["error"]["code"] == "service_forbidden"


def test_group_read_all_inheritance_authorizes_every_frozen_read_mode(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
    tmp_path: Path,
) -> None:
    group = client.post(
        "/groups",
        json={"name": "Evidence opacity group", "group_read_all": True},
        headers=admin_auth_headers,
    )
    assert group.status_code == 201, group.text
    group_id = group.json()["data"]["group_id"]
    project = client.post(
        "/projects",
        json={"name": "Inherited evidence project", "group_id": group_id},
        headers=admin_auth_headers,
    )
    assert project.status_code == 201, project.text
    membership = client.post(
        f"/groups/{group_id}/members",
        json={
            "username": scoped_project_member.member_username,
            "role": "viewer",
        },
        headers=admin_auth_headers,
    )
    assert membership.status_code == 201, membership.text
    records = _create_evidence_artifact_records(
        client,
        admin_auth_headers,
        project_id=project.json()["data"]["project_id"],
        artifact_root=tmp_path,
    )

    cases = [
        case
        for domain_cases in _read_cases(records).values()
        for case in domain_cases
    ]
    assert len(cases) == 15
    for case in cases:
        response = client.get(
            case.existing_path,
            headers=_request_headers(scoped_project_member.member_headers, case),
        )
        assert response.status_code == 200, f"{case.name}: {response.text}"

    for case in _resolver_cases(records):
        response = client.post(
            "/external-artifacts/resolve",
            json=_resolver_payload(case),
            headers=scoped_project_member.member_headers,
        )
        assert response.status_code == 200, (
            f"resolver-{case.entity_type}: {response.text}"
        )


def test_validation_precedence_is_visibility_independent(
    client: TestClient,
    scoped_project_member,
    evidence_artifact_records: EvidenceArtifactRecords,
) -> None:
    records = evidence_artifact_records
    get_pairs = (
        (
            f"/datasets/{records.dataset_id}/files?limit=0",
            f"/datasets/{records.missing_dataset_id}/files?limit=0",
        ),
        (
            f"/claims/{records.claim_id}/versions?limit=0",
            f"/claims/{records.missing_claim_id}/versions?limit=0",
        ),
        (
            (
                f"/claims/{records.claim_id}/versions/diff"
                "?from_version=invalid&to_version=2"
            ),
            (
                f"/claims/{records.missing_claim_id}/versions/diff"
                "?from_version=invalid&to_version=2"
            ),
        ),
        (
            f"/claims/{records.claim_id}/edges?offset=-1",
            f"/claims/{records.missing_claim_id}/edges?offset=-1",
        ),
    )
    for existing_path, missing_path in get_pairs:
        existing = client.get(
            existing_path,
            headers=scoped_project_member.member_headers,
        )
        missing = client.get(
            missing_path,
            headers=scoped_project_member.member_headers,
        )
        assert existing.status_code == missing.status_code == 422, (
            existing_path,
            existing.text,
            missing.text,
        )
        assert existing.json() == missing.json(), existing_path

    resolver_case = _resolver_cases(records)[0]
    resolver_pairs = (
        (
            _resolver_payload(resolver_case, artifact_index=-1),
            _resolver_payload(
                resolver_case,
                entity_id=resolver_case.missing_id,
                artifact_index=-1,
            ),
        ),
        (
            _resolver_payload(resolver_case, max_bytes=0),
            _resolver_payload(
                resolver_case,
                entity_id=resolver_case.missing_id,
                max_bytes=0,
            ),
        ),
        (
            _resolver_payload(resolver_case, byte_start=0),
            _resolver_payload(
                resolver_case,
                entity_id=resolver_case.missing_id,
                byte_start=0,
            ),
        ),
    )
    for existing_payload, missing_payload in resolver_pairs:
        existing = client.post(
            "/external-artifacts/resolve",
            json=existing_payload,
            headers=scoped_project_member.member_headers,
        )
        missing = client.post(
            "/external-artifacts/resolve",
            json=missing_payload,
            headers=scoped_project_member.member_headers,
        )
        assert existing.status_code == missing.status_code == 422, (
            existing.text,
            missing.text,
        )
        assert existing.json() == missing.json()


def test_authorized_child_errors_and_resolver_classification_are_preserved(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    evidence_artifact_records: EvidenceArtifactRecords,
) -> None:
    records = evidence_artifact_records
    missing_file = client.get(
        f"/datasets/{records.dataset_id}/files/{uuid4()}/download",
        headers=admin_auth_headers,
    )
    assert missing_file.status_code == 404
    assert missing_file.json()["error"]["message"] == (
        "Dataset file does not exist."
    )

    missing_version = client.get(
        (
            f"/claims/{records.claim_id}/versions/diff"
            "?from_version=1&to_version=99"
        ),
        headers=admin_auth_headers,
    )
    assert missing_version.status_code == 404
    assert missing_version.json()["error"]["message"] == (
        "Entity version does not exist."
    )

    no_visualization_file = client.get(
        (
            f"/visualizations/{records.visualization_without_asset_id}"
            "/file/download"
        ),
        headers=admin_auth_headers,
    )
    assert no_visualization_file.status_code == 404
    assert no_visualization_file.json()["error"]["message"] == (
        "Visualization file does not exist."
    )

    resolver_case = _resolver_cases(records)[0]
    bad_index = client.post(
        "/external-artifacts/resolve",
        json=_resolver_payload(resolver_case, artifact_index=99),
        headers=admin_auth_headers,
    )
    assert bad_index.status_code == 404
    assert "No external artifact at index 99" in (
        bad_index.json()["error"]["message"]
    )

    wrong_hash = client.post(
        "/external-artifacts/resolve",
        json=_resolver_payload(
            resolver_case,
            content_hash=_sha256(b"wrong"),
        ),
        headers=admin_auth_headers,
    )
    assert wrong_hash.status_code == 422
    assert wrong_hash.json()["error"]["message"] == (
        "content_hash does not match the artifact at the given index."
    )


def test_mutations_keep_permission_errors_and_never_write_file_storage(
    client: TestClient,
    scoped_project_member,
    evidence_artifact_records: EvidenceArtifactRecords,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = evidence_artifact_records
    storage_mutations: list[tuple[str, object]] = []

    def fail_storage_mutation(operation: str):
        def _fail(*args, **kwargs):  # noqa: ANN002, ANN003
            storage_mutations.append((operation, (args, kwargs)))
            raise AssertionError(
                f"storage {operation} must not run before mutation authorization"
            )

        return _fail

    monkeypatch.setattr(
        client.app.state.file_storage_backend,
        "store_stream",
        fail_storage_mutation("store_stream"),
    )
    monkeypatch.setattr(
        client.app.state.file_storage_backend,
        "delete",
        fail_storage_mutation("delete"),
    )

    cases: tuple[
        tuple[str, str, dict[str, object] | None, dict[str, Any] | None],
        ...,
    ] = (
        (
            "PATCH",
            f"/datasets/{records.dataset_id}",
            {"status": "archived", "terminal_reason": "forbidden"},
            None,
        ),
        (
            "DELETE",
            f"/datasets/{records.dataset_id}",
            None,
            None,
        ),
        (
            "POST",
            f"/datasets/{records.dataset_id}/files",
            None,
            {"file": ("forbidden.txt", b"forbidden", "text/plain")},
        ),
        (
            "DELETE",
            (
                f"/datasets/{records.dataset_id}/files/"
                f"{records.dataset_file_id}"
            ),
            None,
            None,
        ),
        (
            "PATCH",
            f"/analyses/{records.analysis_id}",
            {"environment_hash": "forbidden"},
            None,
        ),
        (
            "POST",
            f"/analyses/{records.analysis_id}/commit",
            {"environment_hash": "forbidden"},
            None,
        ),
        (
            "DELETE",
            f"/analyses/{records.analysis_id}",
            None,
            None,
        ),
        (
            "PATCH",
            f"/claims/{records.claim_id}",
            {"statement": "Forbidden mutation"},
            None,
        ),
        (
            "POST",
            f"/claims/{records.claim_id}/edges",
            {
                "target_claim_id": records.target_claim_id,
                "relation": "depends_on",
            },
            None,
        ),
        (
            "DELETE",
            f"/claims/{records.claim_id}",
            None,
            None,
        ),
        (
            "POST",
            f"/visualizations/{records.visualization_id}/file",
            None,
            {"file": ("forbidden.png", b"forbidden", "image/png")},
        ),
        (
            "PATCH",
            f"/visualizations/{records.visualization_id}",
            {"caption": "Forbidden mutation"},
            None,
        ),
        (
            "DELETE",
            f"/visualizations/{records.visualization_id}",
            None,
            None,
        ),
        (
            "PATCH",
            f"/exploration-nodes/{records.exploration_node_id}",
            {"title": "Forbidden mutation"},
            None,
        ),
        (
            "DELETE",
            f"/exploration-nodes/{records.exploration_node_id}",
            None,
            None,
        ),
        (
            "PATCH",
            f"/provenance-links/{records.provenance_link_id}",
            {"status": "accepted"},
            None,
        ),
        (
            "POST",
            "/evidence-bundles",
            {
                "project_id": records.project_id,
                "dataset": {
                    "kind": "existing",
                    "dataset_id": records.dataset_id,
                },
            },
            None,
        ),
    )

    for method, path, payload, files in cases:
        response = client.request(
            method,
            path,
            json=payload,
            files=files,
            headers=scoped_project_member.member_headers,
        )
        assert response.status_code == 401, f"{method} {path}: {response.text}"
        assert response.json()["error"]["code"] == "auth_error", path

    assert storage_mutations == []


def test_denied_roots_precede_children_storage_resolvers_and_usage(
    client: TestClient,
    scoped_project_member,
    evidence_artifact_records: EvidenceArtifactRecords,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = evidence_artifact_records
    calls: list[str] = []

    def fail_repository(name: str):
        def _fail(*args, **kwargs):  # noqa: ANN002, ANN003
            calls.append(name)
            raise AssertionError(f"{name} must not run before root authorization")

        return _fail

    for method_name in (
        "query_dataset_files",
        "query_entity_versions",
        "query_claim_edges",
        "query_claims",
        "query_visualizations",
        "query_supervision_edges",
    ):
        monkeypatch.setattr(
            SQLAlchemyLabTrackerRepository,
            method_name,
            fail_repository(method_name),
        )
    monkeypatch.setattr(
        ContextQueries,
        "_materialize_reference",
        fail_repository("materialize_reference"),
    )
    monkeypatch.setattr(
        client.app.state.file_storage_backend,
        "iter_chunks",
        fail_repository("file_storage"),
    )
    monkeypatch.setattr(
        LabTrackerAPI,
        "record_usage_event",
        fail_repository("usage_event"),
    )

    class FailResolverRegistry:
        def resolve(self, *args, **kwargs):  # noqa: ANN002, ANN003
            calls.append("resolver")
            raise AssertionError("resolver must not run before root authorization")

    client.app.state.resolver_registry = FailResolverRegistry()
    client.app.state.settings.usage_events = True

    cases = [
        case
        for domain_cases in _read_cases(records).values()
        for case in domain_cases
    ]
    assert len(cases) == 15
    for case in cases:
        response = client.get(
            case.existing_path,
            headers=_request_headers(scoped_project_member.member_headers, case),
        )
        assert response.status_code == 404, f"{case.name}: {response.text}"

    for case in _resolver_cases(records):
        response = client.post(
            "/external-artifacts/resolve",
            json=_resolver_payload(case),
            headers=scoped_project_member.member_headers,
        )
        assert response.status_code == 404, (
            f"resolver-{case.entity_type}: {response.text}"
        )

    assert calls == []


def test_claim_provenance_loads_raw_descendants_only_after_root_authorization(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
    evidence_artifact_records: EvidenceArtifactRecords,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = evidence_artifact_records
    analysis_calls: list[UUID] = []
    dataset_calls: list[UUID] = []
    question_calls: list[UUID] = []
    original_get_analysis = AnalysisService.get_analysis
    original_get_dataset = DatasetService.get_dataset
    original_get_question = QuestionService.get_question

    def spy_get_analysis(self, analysis_id: UUID):  # noqa: ANN001
        analysis_calls.append(analysis_id)
        return original_get_analysis(self, analysis_id)

    def spy_get_dataset(self, dataset_id: UUID):  # noqa: ANN001
        dataset_calls.append(dataset_id)
        return original_get_dataset(self, dataset_id)

    def spy_get_question(self, question_id: UUID):  # noqa: ANN001
        question_calls.append(question_id)
        return original_get_question(self, question_id)

    monkeypatch.setattr(AnalysisService, "get_analysis", spy_get_analysis)
    monkeypatch.setattr(DatasetService, "get_dataset", spy_get_dataset)
    monkeypatch.setattr(QuestionService, "get_question", spy_get_question)

    path = f"/claims/{records.claim_id}/provenance"
    outsider = client.get(
        path,
        headers=scoped_project_member.member_headers,
    )
    assert outsider.status_code == 404
    assert outsider.json() == _not_found_body("Claim")
    assert analysis_calls == []
    assert dataset_calls == []
    assert question_calls == []

    authorized = client.get(path, headers=admin_auth_headers)
    assert authorized.status_code == 200, authorized.text
    assert analysis_calls == [UUID(records.analysis_id)]
    assert dataset_calls == [UUID(records.dataset_id)]
    assert question_calls == [UUID(records.question_id)]


def test_provenance_authorizes_parent_before_loading_descendants(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
    evidence_artifact_records: EvidenceArtifactRecords,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = evidence_artifact_records
    calls: list[UUID] = []
    original = DatasetService.get_dataset

    def missing_child(self, dataset_id: UUID):  # noqa: ANN001
        if str(dataset_id) == records.dataset_id:
            calls.append(dataset_id)
            raise NotFoundError("Dataset does not exist.")
        return original(self, dataset_id)

    monkeypatch.setattr(DatasetService, "get_dataset", missing_child)

    outsider = client.get(
        f"/analyses/{records.analysis_id}/provenance",
        headers=scoped_project_member.member_headers,
    )
    assert outsider.status_code == 404
    assert outsider.json() == _not_found_body("Analysis")
    assert calls == []

    authorized = client.get(
        f"/analyses/{records.analysis_id}/provenance",
        headers=admin_auth_headers,
    )
    assert authorized.status_code == 404
    assert authorized.json()["error"]["message"] == "Dataset does not exist."
    assert calls == [UUID(records.dataset_id)]


def test_dangling_visualization_parent_has_stable_authority_classification(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
    evidence_artifact_records: EvidenceArtifactRecords,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = evidence_artifact_records
    original = AnalysisService.get_analysis

    def missing_parent(self, analysis_id: UUID):  # noqa: ANN001
        if str(analysis_id) == records.analysis_id:
            raise NotFoundError("Analysis does not exist.")
        return original(self, analysis_id)

    monkeypatch.setattr(AnalysisService, "get_analysis", missing_parent)
    paths = (
        f"/visualizations/{records.visualization_id}",
        f"/visualizations/{records.visualization_id}/file/download",
    )

    for path in paths:
        outsider = client.get(
            path,
            headers=scoped_project_member.member_headers,
        )
        assert outsider.status_code == 404, outsider.text
        assert outsider.json() == _not_found_body("Visualization")

    membership = client.post(
        f"/projects/{records.project_id}/members",
        json={
            "user_id": scoped_project_member.member_user_id,
            "role": "viewer",
        },
        headers=admin_auth_headers,
    )
    assert membership.status_code == 201, membership.text

    for path in paths:
        project_viewer = client.get(
            path,
            headers=scoped_project_member.member_headers,
        )
        assert project_viewer.status_code == 404, project_viewer.text
        assert project_viewer.json() == _not_found_body("Visualization")

    for path in paths:
        authorized = client.get(path, headers=admin_auth_headers)
        assert authorized.status_code == 404, authorized.text
        assert authorized.json()["error"]["message"] == "Analysis does not exist."
