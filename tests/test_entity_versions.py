from __future__ import annotations

from uuid import uuid4

from api_helpers import repository_backed_api
from fastapi.testclient import TestClient

from lab_tracker.auth import AuthContext, Role
from lab_tracker.models import (
    ClaimStatus,
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    QuestionStatus,
    QuestionType,
)


def _actor(role: Role = Role.ADMIN) -> AuthContext:
    return AuthContext(user_id=uuid4(), role=role)


def test_question_versions_capture_prior_snapshot_and_diff():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Versioned questions", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="What is the baseline?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )

    api.update_question(
        question.question_id,
        text="What is the evoked baseline?",
        hypothesis="Stimulus changes baseline activity.",
        actor=actor,
    )

    versions = api.list_entity_versions(
        entity_type=EntityType.QUESTION,
        entity_id=question.question_id,
    )
    assert [version.version_number for version in versions] == [1, 2]
    assert versions[0].snapshot["text"] == "What is the baseline?"
    assert versions[1].snapshot["text"] == "What is the evoked baseline?"

    diff = api.diff_entity_versions(
        entity_type=EntityType.QUESTION,
        entity_id=question.question_id,
        from_version=1,
        to_version=2,
    )
    assert diff.changed_fields["text"] == {
        "before": "What is the baseline?",
        "after": "What is the evoked baseline?",
    }
    assert diff.changed_fields["hypothesis"] == {
        "before": None,
        "after": "Stimulus changes baseline activity.",
    }


def test_claim_versions_capture_prior_snapshot_and_diff():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Versioned claims", actor=actor)
    claim = api.create_claim(
        project_id=project.project_id,
        statement="The phenotype is stable.",
        confidence=40,
        status=ClaimStatus.PROPOSED,
        actor=actor,
    )

    api.update_claim(
        claim.claim_id,
        statement="The phenotype is state dependent.",
        confidence=65,
        actor=actor,
    )

    versions = api.list_entity_versions(
        entity_type=EntityType.CLAIM,
        entity_id=claim.claim_id,
    )
    assert [version.version_number for version in versions] == [1, 2]
    assert versions[0].snapshot["statement"] == "The phenotype is stable."
    assert versions[1].snapshot["statement"] == "The phenotype is state dependent."

    diff = api.diff_entity_versions(
        entity_type=EntityType.CLAIM,
        entity_id=claim.claim_id,
        from_version=1,
        to_version=2,
    )
    assert diff.changed_fields["statement"] == {
        "before": "The phenotype is stable.",
        "after": "The phenotype is state dependent.",
    }
    assert diff.changed_fields["confidence"] == {"before": 40.0, "after": 65.0}


def test_graph_draft_commit_stamps_entity_version_with_committed_at():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Graph draft versioning", actor=actor)
    note = api.create_note(
        project_id=project.project_id,
        raw_content="Revise the claim after review.",
        actor=actor,
    )
    claim = api.create_claim(
        project_id=project.project_id,
        statement="Original graph-draft claim.",
        confidence=50,
        actor=actor,
    )
    change_set_id = uuid4()
    operation = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=change_set_id,
        sequence=1,
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.CLAIM,
        target_entity_id=claim.claim_id,
        payload={"statement": "Graph-draft revised claim."},
        status=GraphChangeOperationStatus.ACCEPTED,
    )
    api.graph_drafts._save_graph_change_set(  # noqa: SLF001
        GraphChangeSet(
            change_set_id=change_set_id,
            project_id=project.project_id,
            source_note_id=note.note_id,
            source_note_ids=[note.note_id],
            provider="openai",
            model="test-model",
            prompt_version="test-prompt",
            status=GraphChangeSetStatus.READY,
            operations=[operation],
        )
    )

    committed = api.commit_graph_change_set(
        change_set_id,
        message="Commit graph draft version update",
        actor=actor,
    )

    versions = api.list_entity_versions(
        entity_type=EntityType.CLAIM,
        entity_id=claim.claim_id,
    )
    assert versions[-1].change_set_id == change_set_id
    assert versions[-1].committed_at == committed.committed_at
    assert versions[-1].snapshot["statement"] == "Graph-draft revised claim."


def test_question_version_routes_return_snapshots_and_diff(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project = client.post(
        "/projects",
        json={"name": "Question version routes"},
        headers=admin_auth_headers,
    ).json()["data"]
    question = client.post(
        "/questions",
        json={
            "project_id": project["project_id"],
            "text": "Initial route question?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    ).json()["data"]
    response = client.patch(
        f"/questions/{question['question_id']}",
        json={"text": "Updated route question?"},
        headers=admin_auth_headers,
    )
    assert response.status_code == 200

    versions = client.get(
        f"/questions/{question['question_id']}/versions",
        headers=admin_auth_headers,
    )
    assert versions.status_code == 200
    payload = versions.json()
    assert payload["meta"]["total"] == 2
    assert payload["data"][0]["snapshot"]["text"] == "Initial route question?"
    assert payload["data"][1]["snapshot"]["text"] == "Updated route question?"

    diff = client.get(
        f"/questions/{question['question_id']}/versions/diff",
        params={"from_version": 1, "to_version": 2},
        headers=admin_auth_headers,
    )
    assert diff.status_code == 200
    assert diff.json()["data"]["changed_fields"]["text"] == {
        "before": "Initial route question?",
        "after": "Updated route question?",
    }


def test_claim_version_routes_return_snapshots_and_diff(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project = client.post(
        "/projects",
        json={"name": "Claim version routes"},
        headers=admin_auth_headers,
    ).json()["data"]
    claim = client.post(
        "/claims",
        json={
            "project_id": project["project_id"],
            "statement": "Initial route claim.",
            "confidence": 30,
        },
        headers=admin_auth_headers,
    ).json()["data"]
    response = client.patch(
        f"/claims/{claim['claim_id']}",
        json={"statement": "Updated route claim.", "confidence": 70},
        headers=admin_auth_headers,
    )
    assert response.status_code == 200

    versions = client.get(
        f"/claims/{claim['claim_id']}/versions",
        headers=admin_auth_headers,
    )
    assert versions.status_code == 200
    payload = versions.json()
    assert payload["meta"]["total"] == 2
    assert payload["data"][0]["snapshot"]["statement"] == "Initial route claim."
    assert payload["data"][1]["snapshot"]["statement"] == "Updated route claim."

    diff = client.get(
        f"/claims/{claim['claim_id']}/versions/diff",
        params={"from_version": 1, "to_version": 2},
        headers=admin_auth_headers,
    )
    assert diff.status_code == 200
    assert diff.json()["data"]["changed_fields"]["statement"] == {
        "before": "Initial route claim.",
        "after": "Updated route claim.",
    }
