from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from lab_tracker.api import LabTrackerAPI
from lab_tracker.app_parts.middleware import system_auth_context
from lab_tracker.models import (
    ContentHashCarrier,
    EntityRef,
    EntityType,
    Note,
    ProvenanceLink,
    ProvenanceLinkBasis,
    ProvenanceLinkOrigin,
    ProvenanceLinkRelation,
    ProvenanceLinkStatus,
    evidence_content_hash_from_metadata,
)
from lab_tracker.provenance import AraArtifactRecords, build_ara_artifact_document
from lab_tracker.services.provenance_link_service import (
    ProvenanceLinkService,
    group_content_hash_carriers,
)
from lab_tracker.sqlalchemy_repository_parts.repository import SQLAlchemyLabTrackerRepository


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

# --- Unit: hash derivation and the pure grouping helper ------------------


def test_evidence_content_hash_from_metadata_treats_missing_and_empty_as_none() -> None:
    assert evidence_content_hash_from_metadata(None) is None
    assert evidence_content_hash_from_metadata({}) is None
    assert evidence_content_hash_from_metadata({"evidence_content_hash": ""}) is None
    assert evidence_content_hash_from_metadata({"evidence_content_hash": "h"}) == "h"


def test_note_read_exposes_evidence_content_hash_computed_from_metadata() -> None:
    with_hash = Note(
        note_id=uuid4(),
        project_id=uuid4(),
        raw_content="x",
        metadata={"evidence_content_hash": "h"},
    )
    without_hash = Note(note_id=uuid4(), project_id=uuid4(), raw_content="x")

    assert with_hash.model_dump()["evidence_content_hash"] == "h"
    assert without_hash.model_dump()["evidence_content_hash"] is None


def _carrier(
    content_hash: str,
    entity_type: EntityType,
    entity_id: UUID,
    *,
    minute: int,
) -> ContentHashCarrier:
    return ContentHashCarrier(
        content_hash=content_hash,
        entity=EntityRef(entity_type=entity_type, entity_id=entity_id),
        captured_at=datetime(2026, 1, 1, 12, minute, tzinfo=timezone.utc),
    )


def test_group_content_hash_carriers_dedupes_entities_and_drops_singletons() -> None:
    note_a, note_b, dataset = uuid4(), uuid4(), uuid4()
    carriers = [
        _carrier("h", EntityType.NOTE, note_a, minute=1),
        _carrier("h", EntityType.NOTE, note_b, minute=2),
        # A dataset with two identical-checksum files is one carrier, so the
        # repository's two rows must not pair the dataset with itself.
        _carrier("x", EntityType.DATASET, dataset, minute=3),
        _carrier("x", EntityType.DATASET, dataset, minute=4),
        _carrier("y", EntityType.NOTE, note_a, minute=5),
    ]

    groups = group_content_hash_carriers(carriers)

    assert set(groups) == {"h"}
    assert [carrier.entity.entity_id for carrier in groups["h"]] == [note_a, note_b]


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


def _staged_dataset(client: TestClient, headers: dict[str, str], project_id: str) -> str:
    question = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Which acquisition output was reused?",
            "question_type": "descriptive",
        },
        headers=headers,
    )
    assert question.status_code == 201
    response = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question.json()["data"]["question_id"],
        },
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["dataset_id"]


def _upload_dataset_file(
    client: TestClient,
    headers: dict[str, str],
    dataset_id: str,
    content: bytes,
) -> str:
    response = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("acquired.bin", content, "application/octet-stream")},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["checksum"]


def _run_detection(client: TestClient, headers: dict[str, str], project_id: str) -> None:
    client.app.state.graph_draft_client_factory = lambda settings: _FakeBatchDraftClient()
    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id, "user_hint": "detect"},
        headers=headers,
    )
    assert response.status_code == 201


def _enqueue_detection(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
) -> dict[str, Any]:
    """Queue a run-now batch (background drafting on) instead of executing it."""

    client.app.state.graph_draft_client_factory = lambda settings: _FakeBatchDraftClient()
    client.app.state.settings.graph_draft_background_enabled = True
    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id, "user_hint": "detect"},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]


def _process_next_background_run(client: TestClient):
    with client.app.state.db_session_factory() as session:
        api = LabTrackerAPI(
            raw_storage=client.app.state.raw_note_storage,
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=client.app.state.settings,
            surface="background",
        )
        return api.process_next_graph_draft_batch_run(
            draft_client_factory=client.app.state.graph_draft_client_factory,
            app_settings=client.app.state.settings,
            actor=system_auth_context(),
        )


def _links(client: TestClient, headers: dict[str, str], project_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/provenance-links?project_id={project_id}", headers=headers)
    assert response.status_code == 200
    return response.json()["data"]


def _content_hash_carriers(client: TestClient, project_id: str) -> list[ContentHashCarrier]:
    with client.app.state.db_session_factory() as session:
        repository = SQLAlchemyLabTrackerRepository(session)
        return repository.provenance_links.list_content_hash_carriers(UUID(project_id))


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


# --- The carrier query and dataset endpoints ------------------------------


def test_list_content_hash_carriers_returns_only_shared_hashes_across_notes_and_dataset_files(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    content = b"acquired bytes"
    content_hash = hashlib.sha256(content).hexdigest()
    shared_a = _note_with_hash(client, admin_auth_headers, project_id, "a", "h")
    shared_b = _note_with_hash(client, admin_auth_headers, project_id, "b", "h")
    _note_with_hash(client, admin_auth_headers, project_id, "lonely", "x")
    reused = _note_with_hash(client, admin_auth_headers, project_id, "reused", content_hash)
    dataset_id = _staged_dataset(client, admin_auth_headers, project_id)
    assert _upload_dataset_file(client, admin_auth_headers, dataset_id, content) == content_hash
    # Identical carriers in another project never join this project's groups.
    other_project = _project(client, admin_auth_headers)
    _note_with_hash(client, admin_auth_headers, other_project, "elsewhere", "h")
    _note_with_hash(client, admin_auth_headers, other_project, "elsewhere", "x")

    carriers = _content_hash_carriers(client, project_id)

    assert [
        (carrier.content_hash, carrier.entity.entity_type, str(carrier.entity.entity_id))
        for carrier in carriers
    ] == [
        (content_hash, EntityType.NOTE, reused),
        (content_hash, EntityType.DATASET, dataset_id),
        ("h", EntityType.NOTE, shared_a),
        ("h", EntityType.NOTE, shared_b),
    ]
    assert carriers[0].captured_at <= carriers[1].captured_at
    assert carriers[2].captured_at <= carriers[3].captured_at
    assert all(carrier.captured_at.tzinfo is not None for carrier in carriers)


def test_detector_proposes_note_to_dataset_link_from_uploaded_file_checksum(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    content = b"acquisition output"
    content_hash = hashlib.sha256(content).hexdigest()
    note_id = _note_with_hash(client, admin_auth_headers, project_id, "acquired", content_hash)
    dataset_id = _staged_dataset(client, admin_auth_headers, project_id)
    _upload_dataset_file(client, admin_auth_headers, dataset_id, content)

    _run_detection(client, admin_auth_headers, project_id)
    links = _links(client, admin_auth_headers, project_id)

    assert len(links) == 1
    link = links[0]
    assert link["status"] == "proposed"
    assert link["basis"] == "content_hash_match"
    assert link["content_hash"] == content_hash
    assert link["source"]["entity_type"] == "dataset"
    assert link["source"]["entity_id"] == dataset_id
    assert link["target"]["entity_type"] == "note"
    assert link["target"]["entity_id"] == note_id


# --- The detector runs on every batch execution path ----------------------


def test_detector_runs_on_the_queued_worker_path(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note_with_hash(client, admin_auth_headers, project_id, "a", "h")
    _note_with_hash(client, admin_auth_headers, project_id, "b", "h")

    queued = _enqueue_detection(client, admin_auth_headers, project_id)
    assert queued["status"] == "pending"
    # Enqueuing only reserves the run; nothing is proposed until it executes.
    assert _links(client, admin_auth_headers, project_id) == []

    processed = _process_next_background_run(client)

    assert processed is not None
    assert processed.status.value == "ready"
    links = _links(client, admin_auth_headers, project_id)
    assert len(links) == 1
    assert links[0]["status"] == "proposed"


def test_detector_failure_never_fails_the_queued_run(
    client: TestClient, admin_auth_headers: dict[str, str], monkeypatch
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note_with_hash(client, admin_auth_headers, project_id, "a", "h")
    _note_with_hash(client, admin_auth_headers, project_id, "b", "h")

    def _explode(self, project_id, *, actor=None):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(ProvenanceLinkService, "propose_links_from_content_hash", _explode)
    _enqueue_detection(client, admin_auth_headers, project_id)

    processed = _process_next_background_run(client)

    assert processed is not None
    assert processed.status.value == "ready"
    assert _links(client, admin_auth_headers, project_id) == []


def test_detector_runs_once_per_synchronous_run(
    client: TestClient, admin_auth_headers: dict[str, str], monkeypatch
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note_with_hash(client, admin_auth_headers, project_id, "a", "h")
    _note_with_hash(client, admin_auth_headers, project_id, "b", "h")
    calls: list[UUID] = []
    original = ProvenanceLinkService.propose_links_from_content_hash

    def _counting(self, project_id, *, actor=None):
        calls.append(project_id)
        return original(self, project_id, actor=actor)

    monkeypatch.setattr(ProvenanceLinkService, "propose_links_from_content_hash", _counting)

    _run_detection(client, admin_auth_headers, project_id)

    assert calls == [UUID(project_id)]
    assert len(_links(client, admin_auth_headers, project_id)) == 1


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


def test_provenance_link_repository_contract_declares_detector_queries() -> None:
    # The content-hash detector calls provenance_links.list_by_project and
    # list_content_hash_carriers, so the repository protocol (not just the
    # SQLAlchemy class) must declare both with the implementation's signature.
    import inspect
    from typing import get_type_hints

    from lab_tracker.repository import LabTrackerRepository
    from lab_tracker.sqlalchemy_repository_parts.provenance_links import (
        SQLAlchemyProvenanceLinkRepository,
    )

    provenance_links = LabTrackerRepository.provenance_links
    assert isinstance(provenance_links, property)
    assert provenance_links.fget is not None
    contract = get_type_hints(provenance_links.fget)["return"]
    for method_name in ("list_by_project", "list_content_hash_carriers"):
        assert callable(getattr(contract, method_name, None)), method_name
        assert inspect.signature(getattr(contract, method_name)) == inspect.signature(
            getattr(SQLAlchemyProvenanceLinkRepository, method_name)
        ), method_name
