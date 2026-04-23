from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient


def _node_by_id(document: dict[str, object], node_id: str) -> dict[str, object]:
    graph = document["@graph"]
    assert isinstance(graph, list)
    for node in graph:
        assert isinstance(node, dict)
        if node.get("@id") == node_id:
            return node
    raise AssertionError(f"Node not found: {node_id}")


def _create_committed_dataset_with_provenance(
    client: TestClient,
    headers: dict[str, str],
) -> tuple[str, str, str, str, str]:
    project_id = client.post(
        "/projects",
        json={"name": "Provenance project"},
        headers=headers,
    ).json()["data"]["project_id"]
    primary_question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does provenance export preserve the dataset graph?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    secondary_question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "What secondary question should be linked?",
            "question_type": "descriptive",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    note_id = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "linked note",
        },
        headers=headers,
    ).json()["data"]["note_id"]
    session_id = client.post(
        "/sessions",
        json={
            "project_id": project_id,
            "session_type": "operational",
        },
        headers=headers,
    ).json()["data"]["session_id"]
    dataset_id = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": primary_question_id,
            "secondary_question_ids": [secondary_question_id],
            "status": "committed",
            "commit_manifest": {
                "files": [{"path": "raw/data.csv", "checksum": "abc123", "size_bytes": 12}],
                "metadata": {"run": "7"},
                "nwb_metadata": {"Session Description": "baseline"},
                "bids_metadata": {"Name": "Example Dataset"},
                "note_ids": [note_id],
                "source_session_id": session_id,
            },
        },
        headers=headers,
    ).json()["data"]["dataset_id"]
    return project_id, dataset_id, primary_question_id, secondary_question_id, note_id


def test_dataset_provenance_route_exports_json_ld_graph(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    _, dataset_id, primary_question_id, secondary_question_id, note_id = (
        _create_committed_dataset_with_provenance(client, headers)
    )

    dataset_response = client.get(f"/datasets/{dataset_id}", headers=headers)
    assert dataset_response.status_code == 200
    assert dataset_response.headers["content-type"].startswith("application/json")
    assert dataset_response.json()["data"]["dataset_id"] == dataset_id

    response = client.get(f"/datasets/{dataset_id}/provenance", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/ld+json")

    payload = response.json()
    dataset_iri = f"http://testserver/datasets/{dataset_id}"
    commit_iri = f"{dataset_iri}/provenance/commit"
    primary_question_link_iri = f"{dataset_iri}/provenance/question-links/{primary_question_id}"
    secondary_question_link_iri = f"{dataset_iri}/provenance/question-links/{secondary_question_id}"

    dataset_node = _node_by_id(payload, dataset_iri)
    commit_node = _node_by_id(payload, commit_iri)
    primary_question_link = _node_by_id(payload, primary_question_link_iri)
    secondary_question_link = _node_by_id(payload, secondary_question_link_iri)

    assert dataset_node["@type"] == "prov:Entity"
    assert dataset_node["prov:wasGeneratedBy"] == {"@id": commit_iri}
    assert dataset_node["commitHash"]
    assert dataset_node["status"] == "committed"
    assert commit_node["prov:used"] == [
        {"@id": f"{dataset_iri}/provenance/files/raw%2Fdata.csv"}
    ]
    assert commit_node["metadata"] == {"run": "7"}
    assert commit_node["nwbMetadata"] == {"Session Description": "baseline"}
    assert commit_node["bidsMetadata"] == {"Name": "Example Dataset"}
    assert commit_node["note"] == [{"@id": f"http://testserver/notes/{note_id}"}]
    assert commit_node["sourceSession"]["@id"].startswith("http://testserver/sessions/")
    assert commit_node["questionLink"] == [
        {"@id": primary_question_link_iri},
        {"@id": secondary_question_link_iri},
    ]
    assert primary_question_link["question"] == {
        "@id": f"http://testserver/questions/{primary_question_id}"
    }
    assert primary_question_link["role"] == "primary"
    assert secondary_question_link["question"] == {
        "@id": f"http://testserver/questions/{secondary_question_id}"
    }
    assert secondary_question_link["role"] == "secondary"


def test_analysis_provenance_route_exports_related_entities(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    project_id, dataset_id, _, _, _ = _create_committed_dataset_with_provenance(client, headers)

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
    commit_response = client.post(
        f"/analyses/{analysis_id}/commit",
        json={"environment_hash": "env-1"},
        headers=headers,
    )
    assert commit_response.status_code == 200
    claim_id = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "Signal is stable",
            "confidence": 0.8,
            "status": "supported",
            "supported_by_dataset_ids": [dataset_id],
            "supported_by_analysis_ids": [analysis_id],
        },
        headers=headers,
    ).json()["data"]["claim_id"]
    viz_id = client.post(
        "/visualizations",
        json={
            "analysis_id": analysis_id,
            "viz_type": "line",
            "file_path": "figs/signal.png",
            "related_claim_ids": [claim_id],
        },
        headers=headers,
    ).json()["data"]["viz_id"]

    analysis_response = client.get(f"/analyses/{analysis_id}", headers=headers)
    assert analysis_response.status_code == 200
    assert analysis_response.headers["content-type"].startswith("application/json")
    assert analysis_response.json()["data"]["analysis_id"] == analysis_id

    response = client.get(f"/analyses/{analysis_id}/provenance", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/ld+json")

    payload = response.json()
    analysis_iri = f"http://testserver/analyses/{analysis_id}"
    dataset_iri = f"http://testserver/datasets/{dataset_id}"
    claim_iri = f"http://testserver/claims/{claim_id}"
    viz_iri = f"http://testserver/visualizations/{viz_id}"

    analysis_node = _node_by_id(payload, analysis_iri)
    claim_node = _node_by_id(payload, claim_iri)
    viz_node = _node_by_id(payload, viz_iri)
    agent_node = _node_by_id(payload, analysis_node["prov:wasAssociatedWith"]["@id"])

    assert analysis_node["@type"] == "prov:Activity"
    assert analysis_node["prov:used"] == [{"@id": dataset_iri}]
    assert analysis_node["methodHash"] == "method-1"
    assert analysis_node["codeVersion"] == "v1"
    assert analysis_node["environmentHash"] == "env-1"
    assert analysis_node["status"] == "committed"
    assert analysis_node["executedAt"]
    assert agent_node["@type"] == "prov:Agent"
    assert agent_node["userId"]
    assert claim_node["supportsDataset"] == [{"@id": dataset_iri}]
    assert claim_node["supportsAnalysis"] == [{"@id": analysis_iri}]
    assert viz_node["prov:wasGeneratedBy"] == {"@id": analysis_iri}
    assert viz_node["relatedClaim"] == [{"@id": claim_iri}]


def test_provenance_routes_require_authentication(client: TestClient):
    dataset_id = uuid4()
    analysis_id = uuid4()

    dataset_response = client.get(f"/datasets/{dataset_id}/provenance")
    analysis_response = client.get(f"/analyses/{analysis_id}/provenance")

    assert dataset_response.status_code == 401
    assert analysis_response.status_code == 401


def test_provenance_routes_return_not_found_for_missing_resources(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    dataset_id = uuid4()
    analysis_id = uuid4()

    dataset_response = client.get(f"/datasets/{dataset_id}/provenance", headers=headers)
    analysis_response = client.get(f"/analyses/{analysis_id}/provenance", headers=headers)

    assert dataset_response.status_code == 404
    assert analysis_response.status_code == 404
