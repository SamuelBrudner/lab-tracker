"""Worktree-tree provenance proposals: a capture made from a commit's exact code.

A capture that recorded the git tree of the working copy it ran in
(``run_git_worktree_tree``, ``capture_git_worktree_tree``,
``hpc_git_worktree_tree``) was made from exactly the code of any commit whose
own tree (``repo_git_tree``) is the same id. The detector proposes, never
accepts, a ``was_derived_from`` link from the capture to the earliest such
commit note.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from api_helpers import repository_backed_api
from fastapi.testclient import TestClient

from lab_tracker.auth import AuthContext, Role
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
from lab_tracker.services.provenance_detection_stage import _detectors
from lab_tracker.services.provenance_link_service import ProvenanceLinkService
from lab_tracker.services.provenance_tree_matches import (
    COMMIT_TREE_METADATA_KEY,
    TREE_MATCH_METADATA_KEYS,
    WORKTREE_TREE_METADATA_KEYS,
    normalize_tree_id,
    tree_matches_for_notes,
)

TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
OTHER_TREE = "9f2c1d4e5a6b7c8d9e0f1a2b3c4d5e6f70819203"
T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _note(
    project_id: UUID, metadata: dict[str, str], *, minutes: int = 0, note_id: UUID | None = None
) -> Note:
    return Note(
        note_id=note_id or uuid4(),
        project_id=project_id,
        raw_content="capture",
        metadata=metadata,
        created_at=T0 + timedelta(minutes=minutes),
    )


# --- Unit: the pure matching rules ------------------------------------------


def test_the_contract_keys_are_the_ones_the_clients_stamp() -> None:
    assert WORKTREE_TREE_METADATA_KEYS == (
        "capture_git_worktree_tree",
        "run_git_worktree_tree",
        "hpc_git_worktree_tree",
    )
    assert COMMIT_TREE_METADATA_KEY == "repo_git_tree"
    assert (*WORKTREE_TREE_METADATA_KEYS, "repo_git_tree") == TREE_MATCH_METADATA_KEYS


def test_normalize_tree_id_accepts_only_full_sha1_or_sha256_tree_ids() -> None:
    assert normalize_tree_id(f"  {TREE.upper()} ") == TREE
    assert normalize_tree_id("a" * 64) == "a" * 64
    for value in ("", TREE[:12], TREE + "0", "z" * 40, "timeout", None):
        assert normalize_tree_id(value) == "", value


def test_each_worktree_key_links_to_the_earliest_commit_note_with_that_tree() -> None:
    project_id = uuid4()
    later_commit = _note(project_id, {"repo_git_tree": TREE}, minutes=30)
    earliest_commit = _note(project_id, {"repo_git_tree": TREE}, minutes=5)
    captures = [
        _note(project_id, {key: TREE}, minutes=10 + index)
        for index, key in enumerate(WORKTREE_TREE_METADATA_KEYS)
    ]

    matches = tree_matches_for_notes([later_commit, *captures, earliest_commit])

    assert [(m.note_id, m.commit_note_id, m.metadata_key, m.tree) for m in matches] == [
        (capture.note_id, earliest_commit.note_id, key, TREE)
        for capture, key in zip(captures, WORKTREE_TREE_METADATA_KEYS, strict=True)
    ]


def test_the_first_resolving_key_wins_so_a_note_proposes_one_commit() -> None:
    project_id = uuid4()
    commit = _note(project_id, {"repo_git_tree": TREE})
    other_commit = _note(project_id, {"repo_git_tree": OTHER_TREE}, minutes=1)
    both = _note(
        project_id,
        {"capture_git_worktree_tree": TREE, "run_git_worktree_tree": OTHER_TREE},
        minutes=2,
    )
    fallback = _note(
        project_id,
        {"capture_git_worktree_tree": "f" * 40, "run_git_worktree_tree": OTHER_TREE.upper()},
        minutes=3,
    )

    matches = tree_matches_for_notes([commit, other_commit, both, fallback])

    assert [(m.note_id, m.commit_note_id, m.metadata_key) for m in matches] == [
        (both.note_id, commit.note_id, "capture_git_worktree_tree"),
        (fallback.note_id, other_commit.note_id, "run_git_worktree_tree"),
    ]


def test_a_note_never_links_to_itself_but_may_link_to_another_carrier() -> None:
    project_id = uuid4()
    lonely = _note(project_id, {"run_git_worktree_tree": TREE, "repo_git_tree": TREE})
    assert tree_matches_for_notes([lonely]) == []

    later = _note(project_id, {"repo_git_tree": TREE}, minutes=9)
    (match,) = tree_matches_for_notes([lonely, later])
    assert (match.note_id, match.commit_note_id) == (lonely.note_id, later.note_id)


def test_no_commit_with_that_tree_or_an_unusable_value_proposes_nothing() -> None:
    project_id = uuid4()
    notes = [
        _note(project_id, {"repo_git_tree": OTHER_TREE}),
        _note(project_id, {"capture_git_worktree_tree": TREE}, minutes=1),
        _note(project_id, {"run_git_worktree_tree_error": "too_large"}, minutes=2),
        _note(project_id, {"hpc_git_worktree_tree": OTHER_TREE[:12]}, minutes=3),
        _note(project_id, {"repo_git_tree": "not-a-tree", "run_git_worktree_tree": TREE}),
    ]

    assert tree_matches_for_notes(notes) == []


def test_ties_on_capture_time_are_broken_by_note_id() -> None:
    project_id = uuid4()
    first = _note(project_id, {"repo_git_tree": TREE}, note_id=UUID(int=1))
    second = _note(project_id, {"repo_git_tree": TREE}, note_id=UUID(int=2))
    capture = _note(project_id, {"run_git_worktree_tree": TREE}, minutes=1)

    (match,) = tree_matches_for_notes([second, capture, first])

    assert match.commit_note_id == first.note_id


def test_the_detector_runs_in_the_deterministic_stage() -> None:
    class _Links:
        def __getattr__(self, name: str) -> Any:
            return name

    names = [name for name, _detector in _detectors(_Links())]  # type: ignore[arg-type]

    assert "worktree-tree" in names
    assert dict(_detectors(_Links()))["worktree-tree"] == "propose_links_from_tree_matches"  # type: ignore[arg-type]


def test_an_accepted_tree_match_renders_as_was_derived_from() -> None:
    project_id = uuid4()
    commit = Note(note_id=uuid4(), project_id=project_id, raw_content="commit")
    capture = Note(note_id=uuid4(), project_id=project_id, raw_content="figure")
    link = ProvenanceLink(
        link_id=uuid4(),
        project_id=project_id,
        source=EntityRef(entity_type=EntityType.NOTE, entity_id=capture.note_id),
        target=EntityRef(entity_type=EntityType.NOTE, entity_id=commit.note_id),
        relation=ProvenanceLinkRelation.WAS_DERIVED_FROM,
        basis=ProvenanceLinkBasis.WORKTREE_TREE_MATCH,
        status=ProvenanceLinkStatus.ACCEPTED,
        origin=ProvenanceLinkOrigin.SYSTEM_DETECTED,
    )
    records = AraArtifactRecords(
        questions=[],
        datasets=[],
        analyses=[],
        claims=[],
        claim_edges=[],
        notes=[commit, capture],
        visualizations=[],
        entity_versions=[],
        provenance_links=[link],
    )

    document = build_ara_artifact_document(
        "http://testserver",
        scope_type=EntityType.PROJECT,
        scope_id=project_id,
        records=records,
        generated_at=T0,
    )

    capture_node = _node_with_id(document, f"http://testserver/notes/{capture.note_id}")
    assert capture_node is not None
    assert capture_node["wasDerivedFrom"] == [{"@id": f"http://testserver/notes/{commit.note_id}"}]


def _node_with_id(document: object, node_id: str) -> dict[str, Any] | None:
    if isinstance(document, dict):
        if document.get("@id") == node_id and "@type" in document:
            return document
        values: list[object] = list(document.values())
    elif isinstance(document, list):
        values = list(document)
    else:
        return None
    for value in values:
        found = _node_with_id(value, node_id)
        if found is not None:
            return found
    return None


# --- Integration: the detector feeds the provenance-link review surface ------


class _FakeBatchClient:
    provider = "fake"
    model = "fake-batch-model"

    def draft_from_batch(self, *, batch_context: dict[str, Any], user_hint: str | None = None):
        return {
            "summary": "nothing from the model",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [],
        }

    def close(self) -> None:
        pass


def _project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post(
        "/projects", json={"name": f"Tree match {uuid4().hex[:6]}"}, headers=headers
    )
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _staged_note(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    text: str,
    metadata: dict[str, str],
) -> str:
    payload = {"project_id": project_id, "raw_content": text, "status": "staged"}
    response = client.post("/notes", json={**payload, "metadata": metadata}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["note_id"]


def _run_batch(client: TestClient, headers: dict[str, str], project_id: str) -> dict[str, Any]:
    client.app.state.graph_draft_client_factory = lambda settings: _FakeBatchClient()
    run = client.post("/batches/run-now", json={"project_id": project_id}, headers=headers)
    assert run.status_code == 201, run.text
    return run.json()["data"]


def _links(
    client: TestClient, headers: dict[str, str], project_id: str, status: str = "proposed"
) -> list[dict[str, Any]]:
    response = client.get(
        f"/provenance-links?project_id={project_id}&status={status}", headers=headers
    )
    assert response.status_code == 200
    return response.json()["data"]


def test_batch_run_proposes_worktree_tree_links_from_captures_to_their_commit(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    commit_note = _staged_note(
        client, admin_auth_headers, project_id, "Repo commit", {"repo_git_tree": TREE}
    )
    _staged_note(
        client, admin_auth_headers, project_id, "Same tree, later", {"repo_git_tree": TREE}
    )
    figure_note = _staged_note(
        client,
        admin_auth_headers,
        project_id,
        "Figure saved from uncommitted code",
        {"capture_git_worktree_tree": TREE},
    )
    run_note = _staged_note(
        client, admin_auth_headers, project_id, "lt run", {"run_git_worktree_tree": TREE.upper()}
    )
    _staged_note(
        client,
        admin_auth_headers,
        project_id,
        "HPC job from other code",
        {"hpc_git_worktree_tree": OTHER_TREE},
    )

    run = _run_batch(client, admin_auth_headers, project_id)

    assert run["status"] == "ready"
    links = _links(client, admin_auth_headers, project_id)
    assert {link["source"]["entity_id"] for link in links} == {figure_note, run_note}
    for link in links:
        assert link["target"] == {"entity_type": "note", "entity_id": commit_note}
        assert link["source"]["entity_type"] == "note"
        assert link["relation"] == ProvenanceLinkRelation.WAS_DERIVED_FROM.value
        assert link["basis"] == ProvenanceLinkBasis.WORKTREE_TREE_MATCH.value
        assert link["content_hash"] is None
        assert link["status"] == "proposed"
        assert link["origin"] == "system_detected"


def test_worktree_tree_detector_is_idempotent_and_never_reproposes_a_declined_pair(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    _staged_note(client, admin_auth_headers, project_id, "Commit", {"repo_git_tree": TREE})
    _staged_note(
        client, admin_auth_headers, project_id, "HPC begin", {"hpc_git_worktree_tree": TREE}
    )

    _run_batch(client, admin_auth_headers, project_id)
    _run_batch(client, admin_auth_headers, project_id)
    (link,) = _links(client, admin_auth_headers, project_id)

    rejected = client.patch(
        f"/provenance-links/{link['link_id']}",
        json={"status": "rejected"},
        headers=admin_auth_headers,
    )
    assert rejected.status_code == 200, rejected.text
    _run_batch(client, admin_auth_headers, project_id)
    assert _links(client, admin_auth_headers, project_id) == []
    assert len(_links(client, admin_auth_headers, project_id, status="rejected")) == 1


def test_the_service_proposes_through_the_repository_and_counts_new_links() -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    project_id = api.create_project("Tree match service", actor=actor).project_id
    commit = api.create_note(
        project_id=project_id,
        raw_content="Repo commit",
        metadata={"repo_git_tree": TREE},
        actor=actor,
    )
    run = api.create_note(
        project_id=project_id,
        raw_content="lt run",
        metadata={"run_git_worktree_tree": TREE},
        actor=actor,
    )
    api.create_note(
        project_id=project_id,
        raw_content="Nothing to match",
        metadata={"run_git_commit": "abc1234"},
        actor=actor,
    )

    assert api.provenance_links.propose_links_from_tree_matches(project_id, actor=actor) == 1
    assert api.provenance_links.propose_links_from_tree_matches(project_id, actor=actor) == 0
    (link,) = api.list_provenance_links(project_id=project_id)
    assert (link.source.entity_id, link.target.entity_id) == (run.note_id, commit.note_id)
    assert link.basis == ProvenanceLinkBasis.WORKTREE_TREE_MATCH
    assert link.status == ProvenanceLinkStatus.PROPOSED
    assert link.acceptance_mode is None


def test_worktree_tree_detector_failure_never_fails_the_batch_or_other_detectors(
    client: TestClient, admin_auth_headers: dict[str, str], monkeypatch
) -> None:
    project_id = _project(client, admin_auth_headers)
    for text in ("a", "b"):
        _staged_note(
            client,
            admin_auth_headers,
            project_id,
            text,
            {"evidence_content_hash": "h", "run_git_worktree_tree": TREE, "repo_git_tree": TREE},
        )

    def _explode(self, project_id, *, actor=None):
        raise RuntimeError("tree detector exploded")

    monkeypatch.setattr(ProvenanceLinkService, "propose_links_from_tree_matches", _explode)

    run = _run_batch(client, admin_auth_headers, project_id)

    assert run["status"] == "ready"
    links = _links(client, admin_auth_headers, project_id)
    assert [link["basis"] for link in links] == [ProvenanceLinkBasis.CONTENT_HASH_MATCH.value]


# --- Repository: only captures of a committed tree are loaded -----------------


def _carrier_notes(client: TestClient, headers: dict[str, str]) -> tuple[str, list[str]]:
    project_id = _project(client, headers)
    other_project = _project(client, headers)
    created = [
        _staged_note(client, headers, project_id, "run", {"run_git_worktree_tree": TREE}),
        _staged_note(client, headers, project_id, "hpc", {"hpc_git_worktree_tree": OTHER_TREE}),
        _staged_note(client, headers, project_id, "other", {"capture_git_worktree_tree": "a" * 40}),
        _staged_note(client, headers, project_id, "commit", {"repo_git_tree": TREE}),
        _staged_note(client, headers, other_project, "foreign", {"run_git_worktree_tree": TREE}),
    ]
    return project_id, created


def test_metadata_value_carriers_filter_keys_and_values_in_sql(
    client: TestClient, admin_auth_headers: dict[str, str], monkeypatch
) -> None:
    from lab_tracker.sqlalchemy_repository_parts import provenance_links as repository_part
    from lab_tracker.sqlalchemy_repository_parts.repository import (
        SQLAlchemyLabTrackerRepository,
    )

    project_id, created = _carrier_notes(client, admin_auth_headers)
    # One value per IN clause: a note matched by two chunks still comes back once.
    monkeypatch.setattr(repository_part, "_METADATA_VALUE_CHUNK", 1)
    with client.app.state.db_session_factory() as session:
        links = SQLAlchemyLabTrackerRepository(session).provenance_links
        carriers = links.list_metadata_value_carriers(
            UUID(project_id), WORKTREE_TREE_METADATA_KEYS, [OTHER_TREE, TREE, TREE]
        )
        nothing = links.list_metadata_value_carriers(UUID(project_id), (), [TREE])

    assert [str(note.note_id) for note in carriers] == created[:2]
    assert nothing == []


def test_the_repository_contract_declares_the_value_carrier_query() -> None:
    import inspect
    from typing import get_type_hints

    from lab_tracker.repository import LabTrackerRepository
    from lab_tracker.sqlalchemy_repository_parts.provenance_links import (
        SQLAlchemyProvenanceLinkRepository,
    )

    getter = LabTrackerRepository.provenance_links.fget
    assert getter is not None
    contract = get_type_hints(getter)["return"]
    assert inspect.signature(contract.list_metadata_value_carriers) == inspect.signature(
        SQLAlchemyProvenanceLinkRepository.list_metadata_value_carriers
    )


def test_postgres_metadata_value_carriers_filter_in_sql(
    postgres_client: TestClient, postgres_admin_auth_headers: dict[str, str]
) -> None:
    from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

    project_id, created = _carrier_notes(postgres_client, postgres_admin_auth_headers)
    with postgres_client.app.state.db_session_factory() as session:
        carriers = SQLAlchemyLabTrackerRepository(
            session
        ).provenance_links.list_metadata_value_carriers(
            UUID(project_id), WORKTREE_TREE_METADATA_KEYS, [TREE, OTHER_TREE]
        )

    assert [str(note.note_id) for note in carriers] == created[:2]
