"""Exact-id provenance-link proposals: ids a capture already carries, human-gated."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from lab_tracker.models import (
    Analysis,
    EntityRef,
    EntityType,
    Note,
    ProvenanceLinkBasis,
    ProvenanceLinkRelation,
    Session,
    SessionType,
)
from lab_tracker.services.provenance_id_matches import (
    ID_MATCH_COMMIT_METADATA_KEYS,
    ID_MATCH_SESSION_METADATA_KEYS,
    MIN_COMMIT_PREFIX_LENGTH,
    commit_matches,
    id_matches_for_note,
)
from lab_tracker.services.provenance_link_service import ProvenanceLinkService

COMMIT = "9f2c1d4e5a6b7c8d9e0f1a2b3c4d5e6f70819203"

# --- Unit: the pure matching rules ------------------------------------------


def _session(project_id: UUID) -> Session:
    return Session(session_id=uuid4(), project_id=project_id, session_type=SessionType.OPERATIONAL)


def _analysis(project_id: UUID, code_version: str) -> Analysis:
    return Analysis(
        analysis_id=uuid4(),
        project_id=project_id,
        dataset_ids=[],
        method_hash="m",
        code_version=code_version,
    )


def _note(
    project_id: UUID,
    metadata: dict[str, str],
    targets: list[EntityRef] | None = None,
) -> Note:
    return Note(
        note_id=uuid4(),
        project_id=project_id,
        raw_content="capture",
        metadata=metadata,
        targets=list(targets or []),
    )


def test_commit_matches_requires_a_prefix_of_at_least_the_minimum_length() -> None:
    assert commit_matches(COMMIT[:12], COMMIT)
    assert commit_matches(COMMIT, COMMIT[:MIN_COMMIT_PREFIX_LENGTH])
    assert commit_matches(COMMIT.upper(), COMMIT)
    assert not commit_matches(COMMIT[: MIN_COMMIT_PREFIX_LENGTH - 1], COMMIT)
    assert not commit_matches(COMMIT[:12], "0" + COMMIT[1:])


def test_id_matches_name_each_session_and_analysis_once_per_note() -> None:
    project_id = uuid4()
    session = _session(project_id)
    analysis = _analysis(project_id, COMMIT)
    note = _note(
        project_id,
        {
            ID_MATCH_SESSION_METADATA_KEYS[0]: str(session.session_id),
            ID_MATCH_SESSION_METADATA_KEYS[1]: str(session.session_id),
            ID_MATCH_COMMIT_METADATA_KEYS[0]: COMMIT[:12],
            ID_MATCH_COMMIT_METADATA_KEYS[1]: COMMIT,
        },
    )

    matches = id_matches_for_note(note, sessions=[session], analyses=[analysis])

    assert [(m.target.entity_type, m.target.entity_id, m.metadata_key) for m in matches] == [
        (EntityType.SESSION, session.session_id, ID_MATCH_SESSION_METADATA_KEYS[0]),
        (EntityType.ANALYSIS, analysis.analysis_id, ID_MATCH_COMMIT_METADATA_KEYS[0]),
    ]
    assert all(match.note_id == note.note_id for match in matches)


def test_id_matches_stay_silent_for_unknown_ambiguous_short_or_already_linked_ids() -> None:
    project_id = uuid4()
    session = _session(project_id)
    stranger = _session(uuid4())
    twins = [_analysis(project_id, COMMIT), _analysis(project_id, COMMIT[:20] + "ffff")]
    cases = {
        "not a uuid": _note(project_id, {"watch_session_id": "rig-2"}),
        "unknown session": _note(project_id, {"watch_session_id": str(stranger.session_id)}),
        "ambiguous commit": _note(project_id, {"repo_git_commit": COMMIT[:12]}),
        "short commit": _note(project_id, {"git_commit": COMMIT[:5]}),
        "already linked": _note(
            project_id,
            {"capture_session_id": str(session.session_id)},
            [EntityRef(entity_type=EntityType.SESSION, entity_id=session.session_id)],
        ),
    }

    for label, note in cases.items():
        assert id_matches_for_note(note, sessions=[session], analyses=twins) == [], label


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
        "/projects", json={"name": f"Id match {uuid4().hex[:6]}"}, headers=headers
    )
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _question(client: TestClient, headers: dict[str, str], project_id: str) -> str:
    response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does the id match land?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["question_id"]


def _committed_dataset(client: TestClient, headers: dict[str, str], project_id: str) -> str:
    response = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": _question(client, headers, project_id),
            "status": "committed",
            "commit_manifest": {
                "external_artifacts": [
                    {
                        "kind": "entity",
                        "source_system": "s3",
                        "uri": "s3://lab-bucket/run-001/manifest.json",
                        "content_hash": "sha256:manifest001",
                    }
                ]
            },
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["dataset_id"]


def _analysis_with(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    *,
    code_version: str,
    status: str = "committed",
) -> str:
    response = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [_committed_dataset(client, headers, project_id)],
            "method_hash": f"method-{uuid4().hex[:8]}",
            "code_version": code_version,
            "status": status,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["analysis_id"]


def _session_id(client: TestClient, headers: dict[str, str], project_id: str) -> str:
    response = client.post(
        "/sessions",
        json={"project_id": project_id, "session_type": "operational"},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["session_id"]


def _staged_note(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    text: str,
    *,
    metadata: dict[str, str] | None = None,
    targets: list[dict[str, str]] | None = None,
) -> str:
    payload: dict[str, Any] = {"project_id": project_id, "raw_content": text, "status": "staged"}
    if metadata:
        payload["metadata"] = metadata
    if targets:
        payload["targets"] = targets
    response = client.post("/notes", json=payload, headers=headers)
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


def test_batch_run_proposes_exact_id_links_on_the_provenance_review_surface(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    analysis_id = _analysis_with(client, admin_auth_headers, project_id, code_version=COMMIT)
    session_id = _session_id(client, admin_auth_headers, project_id)
    note_id = _staged_note(
        client,
        admin_auth_headers,
        project_id,
        "Figure saved from the analysis run",
        metadata={"run_git_commit": COMMIT[:12], "watch_session_id": session_id},
    )
    _staged_note(client, admin_auth_headers, project_id, "No ids here")

    run = _run_batch(client, admin_auth_headers, project_id)
    assert run["status"] == "ready"
    links = _links(client, admin_auth_headers, project_id)

    assert {(link["target"]["entity_type"], link["target"]["entity_id"]) for link in links} == {
        ("session", session_id),
        ("analysis", analysis_id),
    }
    for link in links:
        assert link["source"] == {"entity_type": "note", "entity_id": note_id}
        assert link["relation"] == ProvenanceLinkRelation.WAS_DERIVED_FROM.value
        assert link["basis"] == ProvenanceLinkBasis.EXACT_ID_MATCH.value
        assert link["content_hash"] is None
        assert link["status"] == "proposed"
        assert link["origin"] == "system_detected"

    # The same review surface decides: accept stamps the human curation triple.
    accepted = client.patch(
        f"/provenance-links/{links[0]['link_id']}",
        json={"status": "accepted"},
        headers=admin_auth_headers,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["acceptance_mode"] == "human_selected"
    assert accepted.json()["data"]["basis"] == ProvenanceLinkBasis.EXACT_ID_MATCH.value


def test_exact_id_detector_is_idempotent_and_never_reproposes_a_declined_pair(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    session_id = _session_id(client, admin_auth_headers, project_id)
    _staged_note(
        client,
        admin_auth_headers,
        project_id,
        "Watched file",
        metadata={"watch_session_id": session_id},
    )

    _run_batch(client, admin_auth_headers, project_id)
    _run_batch(client, admin_auth_headers, project_id)
    (link,) = _links(client, admin_auth_headers, project_id)

    rejected = client.patch(
        f"/provenance-links/{link['link_id']}",
        json={"status": "rejected"},
        headers=admin_auth_headers,
    )
    assert rejected.status_code == 200
    _run_batch(client, admin_auth_headers, project_id)
    assert _links(client, admin_auth_headers, project_id) == []
    assert len(_links(client, admin_auth_headers, project_id, status="rejected")) == 1


def test_exact_id_detector_stays_silent_when_unsure_or_already_linked(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    session_id = _session_id(client, admin_auth_headers, project_id)
    # Two committed analyses share a prefix: the commit is ambiguous. A staged
    # analysis is not a committed fact and never matches.
    _analysis_with(client, admin_auth_headers, project_id, code_version=COMMIT)
    _analysis_with(client, admin_auth_headers, project_id, code_version=COMMIT[:20] + "ffff")
    _analysis_with(
        client, admin_auth_headers, project_id, code_version="abcdef0123", status="staged"
    )
    _staged_note(
        client,
        admin_auth_headers,
        project_id,
        "Already attached to its session",
        metadata={"watch_session_id": session_id},
        targets=[{"entity_type": "session", "entity_id": session_id}],
    )
    _staged_note(
        client,
        admin_auth_headers,
        project_id,
        "Commit prefix matches two analyses",
        metadata={"repo_git_commit": COMMIT[:12]},
    )
    _staged_note(
        client,
        admin_auth_headers,
        project_id,
        "Commit too short to trust",
        metadata={"git_commit": COMMIT[:5]},
    )
    _staged_note(
        client,
        admin_auth_headers,
        project_id,
        "Commit names a staged analysis",
        metadata={"hpc_git_commit": "abcdef0123"},
    )

    _run_batch(client, admin_auth_headers, project_id)

    assert _links(client, admin_auth_headers, project_id) == []


def test_exact_id_detector_failure_never_fails_the_batch_or_the_hash_detector(
    client: TestClient, admin_auth_headers: dict[str, str], monkeypatch
) -> None:
    project_id = _project(client, admin_auth_headers)
    for text in ("a", "b"):
        _staged_note(
            client,
            admin_auth_headers,
            project_id,
            text,
            metadata={"evidence_content_hash": "h"},
        )

    def _explode(self, project_id, *, actor=None):
        raise RuntimeError("id detector exploded")

    monkeypatch.setattr(ProvenanceLinkService, "propose_links_from_id_matches", _explode)

    run = _run_batch(client, admin_auth_headers, project_id)

    assert run["status"] == "ready"
    links = _links(client, admin_auth_headers, project_id)
    assert [link["basis"] for link in links] == [ProvenanceLinkBasis.CONTENT_HASH_MATCH.value]
