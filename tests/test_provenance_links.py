from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from lab_tracker.models import (
    EntityRef,
    EntityType,
    Note,
    ProvenanceLink,
    ProvenanceLinkBasis,
    ProvenanceLinkOrigin,
    ProvenanceLinkRelation,
    ProvenanceLinkStatus,
)
from lab_tracker.provenance import AraArtifactRecords, build_ara_artifact_document
from lab_tracker.services.provenance_link_service import notes_sharing_content_hash


def _find_node_with_id_suffix(document: object, suffix: str) -> dict[str, Any] | None:
    """Recursively find a JSON-LD node whose @id ends with ``suffix``."""
    if isinstance(document, dict):
        node_id = document.get("@id")
        if isinstance(node_id, str) and node_id.endswith(suffix):
            return document
        for value in document.values():
            found = _find_node_with_id_suffix(value, suffix)
            if found is not None:
                return found
    elif isinstance(document, list):
        for item in document:
            found = _find_node_with_id_suffix(item, suffix)
            if found is not None:
                return found
    return None

# --- Unit: the pure grouping helper ---------------------------------------


def _note(content_hash: str | None, *, minute: int) -> Note:
    metadata = {"evidence_content_hash": content_hash} if content_hash is not None else {}
    return Note(
        note_id=uuid4(),
        project_id=uuid4(),
        raw_content="x",
        metadata=metadata,
        created_at=datetime(2026, 1, 1, 12, minute, tzinfo=timezone.utc),
    )


def test_groups_by_hash_drops_singletons_and_empty_hashes() -> None:
    shared_a = _note("hash-a", minute=1)
    shared_b = _note("hash-a", minute=2)
    lonely = _note("hash-b", minute=3)
    no_hash = _note(None, minute=4)

    groups = notes_sharing_content_hash([shared_a, shared_b, lonely, no_hash])

    assert set(groups) == {"hash-a"}
    assert [note.note_id for note in groups["hash-a"]] == [shared_a.note_id, shared_b.note_id]


def test_groups_sort_each_group_by_created_at_ascending() -> None:
    later = _note("h", minute=9)
    earlier = _note("h", minute=1)

    groups = notes_sharing_content_hash([later, earlier])

    assert [note.note_id for note in groups["h"]] == [earlier.note_id, later.note_id]


# --- Integration: detector + human gate via the API -----------------------


class _FakeBatchDraftClient:
    provider = "fake"
    model = "fake-batch-model"

    def __init__(self) -> None:
        self.closed = False

    def draft_from_batch(self, *, batch_context: dict[str, Any], user_hint: str | None = None):
        return {
            "summary": "empty",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [],
        }

    def close(self) -> None:
        self.closed = True


def _project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/projects", json={"name": "Lineage Project"}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _note_with_hash(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    text: str,
    content_hash: str,
) -> str:
    response = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": text,
            "status": "staged",
            "metadata": {"evidence_content_hash": content_hash},
        },
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["note_id"]


def _run_detection(client: TestClient, headers: dict[str, str], project_id: str) -> None:
    client.app.state.graph_draft_client_factory = lambda settings: _FakeBatchDraftClient()
    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id, "user_hint": "detect"},
        headers=headers,
    )
    assert response.status_code == 201


def _links(client: TestClient, headers: dict[str, str], project_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/provenance-links?project_id={project_id}", headers=headers)
    assert response.status_code == 200
    return response.json()["data"]


def test_detector_proposes_derived_from_link_with_correct_direction(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    antecedent = _note_with_hash(client, admin_auth_headers, project_id, "acquired", "sha-shared")
    derived = _note_with_hash(client, admin_auth_headers, project_id, "analyzed", "sha-shared")

    _run_detection(client, admin_auth_headers, project_id)
    links = _links(client, admin_auth_headers, project_id)

    assert len(links) == 1
    link = links[0]
    assert link["status"] == "proposed"
    assert link["origin"] == "system_detected"
    assert link["basis"] == "content_hash_match"
    assert link["content_hash"] == "sha-shared"
    assert link["relation"] == ProvenanceLinkRelation.WAS_DERIVED_FROM.value
    assert link["source"]["entity_id"] == derived
    assert link["target"]["entity_id"] == antecedent
    assert link["acceptance_mode"] is None


def test_accept_stamps_human_curation_provenance(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note_with_hash(client, admin_auth_headers, project_id, "a", "h")
    _note_with_hash(client, admin_auth_headers, project_id, "b", "h")
    _run_detection(client, admin_auth_headers, project_id)
    link_id = _links(client, admin_auth_headers, project_id)[0]["link_id"]

    response = client.patch(
        f"/provenance-links/{link_id}",
        json={"status": "accepted"},
        headers=admin_auth_headers,
    )
    assert response.status_code == 200
    accepted = response.json()["data"]
    assert accepted["status"] == "accepted"
    assert accepted["acceptance_mode"] == "human_selected"
    assert accepted["accepted_at"] is not None


def test_reject_clears_acceptance_and_is_not_reproposed(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note_with_hash(client, admin_auth_headers, project_id, "a", "h")
    _note_with_hash(client, admin_auth_headers, project_id, "b", "h")
    _run_detection(client, admin_auth_headers, project_id)
    link_id = _links(client, admin_auth_headers, project_id)[0]["link_id"]

    rejected = client.patch(
        f"/provenance-links/{link_id}",
        json={"status": "rejected"},
        headers=admin_auth_headers,
    ).json()["data"]
    assert rejected["status"] == "rejected"
    assert rejected["acceptance_mode"] is None
    assert rejected["accepted_at"] is None

    # Re-running detection must not re-propose a declined pair.
    _run_detection(client, admin_auth_headers, project_id)
    statuses = sorted(link["status"] for link in _links(client, admin_auth_headers, project_id))
    assert statuses == ["rejected"]


def test_detection_is_idempotent(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note_with_hash(client, admin_auth_headers, project_id, "a", "h")
    _note_with_hash(client, admin_auth_headers, project_id, "b", "h")
    _run_detection(client, admin_auth_headers, project_id)
    _run_detection(client, admin_auth_headers, project_id)

    assert len(_links(client, admin_auth_headers, project_id)) == 1


def test_three_notes_same_hash_use_star_topology(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    first = _note_with_hash(client, admin_auth_headers, project_id, "a", "h")
    _note_with_hash(client, admin_auth_headers, project_id, "b", "h")
    _note_with_hash(client, admin_auth_headers, project_id, "c", "h")
    _run_detection(client, admin_auth_headers, project_id)

    links = _links(client, admin_auth_headers, project_id)
    # Star, not all-pairs: 3 notes -> 2 links, both pointing at the earliest.
    assert len(links) == 2
    assert all(link["target"]["entity_id"] == first for link in links)


def test_no_public_create_endpoint(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    response = client.post(
        "/provenance-links",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )
    assert response.status_code in {404, 405}


def test_status_update_rejects_illegal_value(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note_with_hash(client, admin_auth_headers, project_id, "a", "h")
    _note_with_hash(client, admin_auth_headers, project_id, "b", "h")
    _run_detection(client, admin_auth_headers, project_id)
    link_id = _links(client, admin_auth_headers, project_id)[0]["link_id"]

    # "proposed" is not an allowed target status.
    response = client.patch(
        f"/provenance-links/{link_id}",
        json={"status": "proposed"},
        headers=admin_auth_headers,
    )
    assert response.status_code == 422


def test_cross_project_links_are_scoped_out_of_list(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note_with_hash(client, admin_auth_headers, project_id, "a", "h")
    _note_with_hash(client, admin_auth_headers, project_id, "b", "h")
    _run_detection(client, admin_auth_headers, project_id)

    other_project = _project(client, admin_auth_headers)
    other_links = _links(client, admin_auth_headers, other_project)
    assert other_links == []


# --- PROV-O rendering of accepted links -----------------------------------


def _ara_records(notes: list[Note], links: list[ProvenanceLink]) -> AraArtifactRecords:
    return AraArtifactRecords(
        questions=[],
        datasets=[],
        analyses=[],
        claims=[],
        claim_edges=[],
        notes=notes,
        visualizations=[],
        entity_versions=[],
        provenance_links=links,
    )


def _link(source: Note, target: Note, status: ProvenanceLinkStatus) -> ProvenanceLink:
    return ProvenanceLink(
        link_id=uuid4(),
        project_id=source.project_id,
        source=EntityRef(entity_type=EntityType.NOTE, entity_id=source.note_id),
        target=EntityRef(entity_type=EntityType.NOTE, entity_id=target.note_id),
        relation=ProvenanceLinkRelation.WAS_DERIVED_FROM,
        basis=ProvenanceLinkBasis.CONTENT_HASH_MATCH,
        content_hash="h",
        status=status,
        origin=ProvenanceLinkOrigin.SYSTEM_DETECTED,
    )


def test_accepted_link_renders_was_derived_from_in_ara_export() -> None:
    project_id = uuid4()
    antecedent = Note(note_id=uuid4(), project_id=project_id, raw_content="acquired")
    derived = Note(note_id=uuid4(), project_id=project_id, raw_content="analyzed")
    records = _ara_records(
        [antecedent, derived],
        [_link(derived, antecedent, ProvenanceLinkStatus.ACCEPTED)],
    )

    document = build_ara_artifact_document(
        "http://testserver",
        scope_type=EntityType.PROJECT,
        scope_id=project_id,
        records=records,
        generated_at=datetime(2026, 6, 26, tzinfo=timezone.utc),
    )

    derived_node = _find_node_with_id_suffix(document, f"/notes/{derived.note_id}")
    assert derived_node is not None
    refs = derived_node.get("wasDerivedFrom")
    assert refs == [{"@id": f"http://testserver/notes/{antecedent.note_id}"}]
    # The antecedent note has no derivation edge.
    antecedent_node = _find_node_with_id_suffix(document, f"/notes/{antecedent.note_id}")
    assert antecedent_node is not None
    assert "wasDerivedFrom" not in antecedent_node


def test_proposed_link_does_not_render_in_ara_export() -> None:
    project_id = uuid4()
    antecedent = Note(note_id=uuid4(), project_id=project_id, raw_content="a")
    derived = Note(note_id=uuid4(), project_id=project_id, raw_content="b")
    records = _ara_records(
        [antecedent, derived],
        [_link(derived, antecedent, ProvenanceLinkStatus.PROPOSED)],
    )

    document = build_ara_artifact_document(
        "http://testserver",
        scope_type=EntityType.PROJECT,
        scope_id=project_id,
        records=records,
        generated_at=datetime(2026, 6, 26, tzinfo=timezone.utc),
    )

    derived_node = _find_node_with_id_suffix(document, f"/notes/{derived.note_id}")
    assert derived_node is not None
    assert "wasDerivedFrom" not in derived_node
