"""HTTP contract for claim reads (derived effective status) and claim-edge deletion."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

DERIVED_FIELDS = (
    "effective_status",
    "superseded_by_claim_id",
    "contested_by_claim_ids",
    "invalidated_by_node_id",
    "pre_registered",
)


def _project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/projects", json={"name": "Claim routes"}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _claim(client: TestClient, headers: dict[str, str], project_id: str, statement: str) -> dict:
    response = client.post(
        "/claims",
        json={"project_id": project_id, "statement": statement, "confidence": 60},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _edge(
    client: TestClient,
    headers: dict[str, str],
    source_id: str,
    target_id: str,
    relation: str,
) -> dict:
    response = client.post(
        f"/claims/{source_id}/edges",
        json={"target_claim_id": target_id, "relation": relation},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_delete_claim_edge_route_returns_edge_then_404_on_repeat(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    source = _claim(client, admin_auth_headers, project_id, "Source claim")
    target = _claim(client, admin_auth_headers, project_id, "Target claim")
    edge = _edge(client, admin_auth_headers, source["claim_id"], target["claim_id"], "extends")

    response = client.delete(
        f"/claims/{source['claim_id']}/edges/{edge['edge_id']}",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"] == edge
    listing = client.get(f"/claims/{source['claim_id']}/edges", headers=admin_auth_headers)
    assert listing.json()["data"] == []
    repeat = client.delete(
        f"/claims/{source['claim_id']}/edges/{edge['edge_id']}",
        headers=admin_auth_headers,
    )
    assert repeat.status_code == 404, repeat.text


def test_delete_claim_edge_route_404_when_edge_belongs_to_other_claim(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    source = _claim(client, admin_auth_headers, project_id, "Source claim")
    target = _claim(client, admin_auth_headers, project_id, "Target claim")
    edge = _edge(client, admin_auth_headers, source["claim_id"], target["claim_id"], "refutes")

    # Addressed through the target claim: the edge is not this claim's, so it reads as absent.
    wrong_owner = client.delete(
        f"/claims/{target['claim_id']}/edges/{edge['edge_id']}",
        headers=admin_auth_headers,
    )
    assert wrong_owner.status_code == 404, wrong_owner.text
    unknown_edge = client.delete(
        f"/claims/{source['claim_id']}/edges/{uuid4()}",
        headers=admin_auth_headers,
    )
    assert unknown_edge.status_code == 404, unknown_edge.text
    still_there = client.get(f"/claims/{source['claim_id']}/edges", headers=admin_auth_headers)
    assert [item["edge_id"] for item in still_there.json()["data"]] == [edge["edge_id"]]


def test_claim_reads_include_effective_status_fields(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    old = _claim(client, admin_auth_headers, project_id, "Old claim")
    new = _claim(client, admin_auth_headers, project_id, "Newer claim")
    for payload in (old, new):
        assert set(DERIVED_FIELDS) <= set(payload)
        assert payload["effective_status"] == "proposed"
        assert payload["pre_registered"] is False

    edge = _edge(client, admin_auth_headers, new["claim_id"], old["claim_id"], "supersedes")

    detail = client.get(f"/claims/{old['claim_id']}", headers=admin_auth_headers)
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert data["status"] == "proposed", "stored status is untouched"
    assert data["effective_status"] == "superseded"
    assert data["superseded_by_claim_id"] == new["claim_id"]
    assert data["contested_by_claim_ids"] == []
    assert data["invalidated_by_node_id"] is None
    assert detail.json()["meta"]["iri"].endswith(f"/claims/{old['claim_id']}")

    listing = client.get("/claims", params={"project_id": project_id}, headers=admin_auth_headers)
    assert listing.status_code == 200, listing.text
    by_id = {item["claim_id"]: item for item in listing.json()["data"]}
    assert by_id[old["claim_id"]]["effective_status"] == "superseded"
    assert by_id[new["claim_id"]]["effective_status"] == "proposed"
    assert all(set(DERIVED_FIELDS) <= set(item) for item in by_id.values())

    patched = client.patch(
        f"/claims/{old['claim_id']}",
        json={"status": "testing"},
        headers=admin_auth_headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["status"] == "testing"
    assert patched.json()["data"]["effective_status"] == "superseded"

    removed = client.delete(
        f"/claims/{new['claim_id']}/edges/{edge['edge_id']}",
        headers=admin_auth_headers,
    )
    assert removed.status_code == 200, removed.text
    reverted = client.get(f"/claims/{old['claim_id']}", headers=admin_auth_headers).json()["data"]
    assert reverted["effective_status"] == "testing"
    assert reverted["superseded_by_claim_id"] is None


def test_claim_read_reports_contest_and_committed_pivot_invalidation(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    claim = _claim(client, admin_auth_headers, project_id, "Contested claim")
    refuter = _claim(client, admin_auth_headers, project_id, "Refuting claim")
    _edge(client, admin_auth_headers, refuter["claim_id"], claim["claim_id"], "refutes")

    contested = client.get(f"/claims/{claim['claim_id']}", headers=admin_auth_headers).json()
    assert contested["data"]["effective_status"] == "contested"
    assert contested["data"]["contested_by_claim_ids"] == [refuter["claim_id"]]

    pivot = client.post(
        "/exploration-nodes",
        json={
            "project_id": project_id,
            "node_type": "pivot",
            "title": "Abandon the contested claim",
            "target": {"entity_type": "claim", "entity_id": claim["claim_id"]},
            "status": "committed",
            "trigger": "The replication failed.",
            "rationale": "The effect did not survive a larger cohort.",
            "invalidates_claim_id": claim["claim_id"],
        },
        headers=admin_auth_headers,
    )
    assert pivot.status_code == 201, pivot.text

    invalidated = client.get(f"/claims/{claim['claim_id']}", headers=admin_auth_headers).json()
    assert invalidated["data"]["effective_status"] == "invalidated"
    assert invalidated["data"]["invalidated_by_node_id"] == pivot.json()["data"]["node_id"]
    assert invalidated["data"]["contested_by_claim_ids"] == [refuter["claim_id"]]
