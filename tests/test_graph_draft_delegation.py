"""Delegated curation: the one path that records ``auto_accepted``.

A project owner may let AI apply part of its own drafting without review, at
an interactive session and with an explicit acknowledgement. Two principals
act under that grant — the drafting pass right after generation, and a
``graph_curate``-scoped token — and only for the operations the grant admits.
Everything else stays exactly as gated as before.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from test_draft_quality import T0, _row
from test_graph_draft_batches import FakeBatchDraftClient, _note, _registered_user
from test_graph_drafts import FakeDraftClient, _image_note, _project

from lab_tracker.api import LabTrackerAPI
from lab_tracker.app import create_app
from lab_tracker.app_parts.middleware import system_auth_context
from lab_tracker.auth import (
    PAT_SCOPE_ALL,
    PAT_SCOPE_GRAPH_CURATE,
    PAT_SCOPE_STAGE_EVIDENCE,
    AuthContext,
    PrincipalType,
    Role,
    service_principal_can_access,
    utc_now,
)
from lab_tracker.draft_quality import aggregate_draft_quality
from lab_tracker.errors import PermissionDeniedError, ServiceScopeDeniedError, ValidationError
from lab_tracker.models import (
    AcceptanceMode,
    DelegatedCurationPolicy,
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftPurpose,
    GraphDraftSemanticType,
    ReviewEmailDeliveryStatus,
)
from lab_tracker.services.graph_draft_commit import TransactionalDraftCommitCoordinator
from lab_tracker.services.graph_draft_delegation import (
    DELEGATED_CURATION_ACKNOWLEDGEMENT_REQUIRED,
    DELEGATED_CURATION_ERROR_KEY,
    DELEGATED_CURATION_PACKET_KEY,
    DELEGATED_CURATION_PROJECT_LEVEL_ONLY,
    FULL_SEMANTIC_TYPES,
    ORGANIZE_SEMANTIC_TYPES,
    admitted_semantic_types,
    delegated_curation_actor,
    is_delegable_principal,
    policy_admits,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker.sqlalchemy_repository_parts.graph_drafts import operation_to_model

_ID = "0f5b2c1e-3a4d-4b6c-8d7e-9f0a1b2c3d4e"
_MIGRATION_PREVIOUS = "0065_note_hash_and_external_context_policy"
_MIGRATION = "0066_delegated_curation"
_MIGRATION_COLUMNS = {
    "delegated_curation",
    "delegated_curation_granted_at",
    "delegated_curation_granted_by",
}


# --- helpers ------------------------------------------------------------------


def _question(client: TestClient, headers: dict[str, str], project_id: str) -> str:
    response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does the protocol improve yield?",
            "question_type": "descriptive",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["question_id"]


def _link_op(note_id: str, question_id: str) -> dict[str, Any]:
    return {
        "client_ref": "link1",
        "op": "update",
        "entity_type": "note",
        "semantic_type": "link_note_to_question",
        "target_entity_id": note_id,
        "payload_json": json.dumps(
            {"targets": [{"entity_type": "question", "entity_id": question_id}]}
        ),
        "rationale": "The note is about this question.",
        "confidence": 0.9,
        "source_refs": [],
    }


def _question_op(project_id: str) -> dict[str, Any]:
    return {
        "client_ref": "q1",
        "op": "create",
        "entity_type": "question",
        "semantic_type": "suggest_new_question",
        "target_entity_id": None,
        "payload_json": json.dumps(
            {
                "project_id": project_id,
                "text": "Does the new whiteboard protocol improve yield?",
                "question_type": "descriptive",
                "status": "staged",
            }
        ),
        "rationale": "The whiteboard states a protocol question.",
        "confidence": 0.82,
        "source_refs": [],
    }


def _patch(*operations: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": "Drafted links",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": list(operations),
    }


def _settings_path(project_id: str) -> str:
    return f"/projects/{project_id}/graph-draft-batch-settings/project-default"


def _grant(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    policy: str,
) -> dict[str, Any]:
    response = client.patch(
        _settings_path(project_id),
        json={"delegated_curation": policy, "delegated_curation_acknowledged": True},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _token_headers(
    client: TestClient,
    headers: dict[str, str],
    *,
    scope: str,
    role: str = "admin",
) -> dict[str, str]:
    # Admin-minted tokens keep the admin role: an admin's project access comes
    # from the role, not from membership, so an editor token would be refused.
    response = client.post(
        "/auth/tokens",
        json={
            "label": f"curator-{scope}",
            "role": role,
            "read_only": False,
            "scope": scope,
            "expires_at": (utc_now() + timedelta(days=7)).isoformat(),
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["scope"] == scope
    return {"Authorization": f"Bearer {data['secret']}"}


def _note_draft(
    client: TestClient,
    headers: dict[str, str],
    note_id: str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(patch)
    response = client.post(f"/notes/{note_id}/graph-drafts", headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _linked_setup(
    client: TestClient, headers: dict[str, str]
) -> tuple[str, str, str]:
    project_id = _project(client, headers)
    note_id = _image_note(client, headers, project_id)
    question_id = _question(client, headers, project_id)
    return project_id, note_id, question_id


def _read_draft(client: TestClient, headers: dict[str, str], change_set_id: str) -> dict[str, Any]:
    response = client.get(f"/graph-drafts/{change_set_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _by_semantic(draft: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {operation["semantic_type"]: operation for operation in draft["operations"]}


@contextmanager
def _request_api(client: TestClient):
    session = client.app.state.db_session_factory()
    try:
        repository = SQLAlchemyLabTrackerRepository(session)
        yield client.app.state.lab_tracker_api.for_request(repository)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _service_actor(scope: str) -> AuthContext:
    return AuthContext(
        user_id=uuid4(),
        role=Role.ADMIN,
        principal_type=PrincipalType.SERVICE,
        service_scope=scope,
    )


def _operation(
    semantic_type: GraphDraftSemanticType | None,
    payload: dict[str, Any] | None = None,
) -> GraphChangeOperation:
    return GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=0,
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.NOTE,
        semantic_type=semantic_type,
        payload=payload or {},
    )


# --- pure policy ----------------------------------------------------------------


def test_organize_admits_only_links_and_full_admits_everything_but_clarifications() -> None:
    assert admitted_semantic_types(DelegatedCurationPolicy.OFF) == frozenset()
    assert admitted_semantic_types(DelegatedCurationPolicy.ORGANIZE) == ORGANIZE_SEMANTIC_TYPES
    assert {
        GraphDraftSemanticType.LINK_NOTE_TO_QUESTION,
        GraphDraftSemanticType.LINK_NOTE_TO_SESSION,
        GraphDraftSemanticType.LINK_NOTE_TO_DATASET,
        GraphDraftSemanticType.LINK_NOTE_TO_ANALYSIS,
        GraphDraftSemanticType.LINK_NODE_TO_GOAL,
    } == ORGANIZE_SEMANTIC_TYPES
    assert admitted_semantic_types(DelegatedCurationPolicy.FULL) == FULL_SEMANTIC_TYPES
    assert GraphDraftSemanticType.REQUEST_CLARIFICATION not in FULL_SEMANTIC_TYPES
    assert FULL_SEMANTIC_TYPES | {GraphDraftSemanticType.REQUEST_CLARIFICATION} == set(
        GraphDraftSemanticType
    )
    for policy in DelegatedCurationPolicy:
        assert not policy_admits(policy, _operation(None)), policy
        assert not policy_admits(
            policy, _operation(GraphDraftSemanticType.REQUEST_CLARIFICATION)
        ), policy
    assert policy_admits(
        DelegatedCurationPolicy.ORGANIZE,
        _operation(GraphDraftSemanticType.LINK_NODE_TO_GOAL, payload={"links": []}),
    )
    assert not policy_admits(
        DelegatedCurationPolicy.ORGANIZE, _operation(GraphDraftSemanticType.SUGGEST_NEW_QUESTION)
    )
    assert policy_admits(
        DelegatedCurationPolicy.FULL, _operation(GraphDraftSemanticType.RESOLVE_PREDICTION)
    )


def test_organize_admits_a_link_only_when_its_payload_only_links() -> None:
    # The link labels map to generic note and goal updates, so a payload that
    # also sets a note status or a goal title is not organizing.
    link = _operation(
        GraphDraftSemanticType.LINK_NOTE_TO_QUESTION,
        payload={"targets": [{"entity_type": "question", "entity_id": _ID}]},
    )
    assert policy_admits(DelegatedCurationPolicy.ORGANIZE, link)
    smuggled_status = _operation(
        GraphDraftSemanticType.LINK_NOTE_TO_QUESTION,
        payload={"targets": [], "status": "committed"},
    )
    assert not policy_admits(DelegatedCurationPolicy.ORGANIZE, smuggled_status)
    assert policy_admits(DelegatedCurationPolicy.FULL, smuggled_status)
    smuggled_transcript = _operation(
        GraphDraftSemanticType.LINK_NOTE_TO_SESSION,
        payload={"targets": [], "transcribed_text": "rewritten"},
    )
    assert not policy_admits(DelegatedCurationPolicy.ORGANIZE, smuggled_transcript)
    goal_title = _operation(
        GraphDraftSemanticType.LINK_NODE_TO_GOAL, payload={"links": [], "title": "renamed"}
    )
    assert not policy_admits(DelegatedCurationPolicy.ORGANIZE, goal_title)


def test_only_the_drafting_pass_and_graph_curate_tokens_may_act_on_a_grant() -> None:
    assert is_delegable_principal(system_auth_context())
    assert is_delegable_principal(delegated_curation_actor())
    assert is_delegable_principal(_service_actor(PAT_SCOPE_GRAPH_CURATE))
    assert not is_delegable_principal(_service_actor(PAT_SCOPE_ALL))
    assert not is_delegable_principal(_service_actor(PAT_SCOPE_STAGE_EVIDENCE))
    assert not is_delegable_principal(AuthContext(user_id=uuid4(), role=Role.ADMIN))
    assert not is_delegable_principal(None)
    assert delegated_curation_actor().is_system
    assert not delegated_curation_actor().is_interactive


# --- token scope allow-list ------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("GET", "/projects"),
        ("POST", "/notes"),
        ("POST", f"/notes/{_ID}/graph-drafts"),
        ("PATCH", f"/notes/{_ID}"),
        ("POST", "/batches/run-now"),
        ("PATCH", f"/graph-drafts/{_ID}/operations/{_ID}"),
        ("POST", f"/graph-drafts/{_ID}/accept-all"),
        ("POST", f"/graph-drafts/{_ID}/commit"),
    ),
)
def test_graph_curate_scope_opens_capture_and_the_review_gate(method: str, path: str) -> None:
    assert service_principal_can_access(
        method, path, read_only=False, role=Role.EDITOR, scope=PAT_SCOPE_GRAPH_CURATE
    )


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("POST", "/questions"),
        ("POST", "/datasets"),
        ("POST", "/claims"),
        ("POST", "/batches/run-due"),
        ("POST", f"/graph-drafts/{_ID}/submit"),
        ("POST", f"/graph-drafts/{_ID}/review"),
        ("POST", f"/graph-drafts/{_ID}/revise"),
        ("POST", f"/graph-drafts/{_ID}/accept-all/extra"),
        ("POST", f"/graph-drafts/{_ID}/commit/"),
        ("PATCH", f"/graph-drafts/{_ID}/operations/"),
        ("PATCH", f"/graph-drafts/{_ID}"),
        ("DELETE", f"/graph-drafts/{_ID}/operations/{_ID}"),
        ("POST", f"/notes/{_ID}/archive"),
        ("PATCH", f"/projects/{_ID}/graph-draft-batch-settings/project-default"),
        ("GET", "/auth/me"),
        ("POST", "/auth/tokens"),
    ),
)
def test_graph_curate_scope_keeps_every_other_write_closed(method: str, path: str) -> None:
    assert not service_principal_can_access(
        method, path, read_only=False, role=Role.ADMIN, scope=PAT_SCOPE_GRAPH_CURATE
    )


def test_graph_curate_scope_respects_read_only_and_viewer_role() -> None:
    for read_only, role in ((True, Role.EDITOR), (False, Role.VIEWER)):
        assert service_principal_can_access(
            "GET", "/projects", read_only=read_only, role=role, scope=PAT_SCOPE_GRAPH_CURATE
        )
        for method, path in (
            ("POST", "/batches/run-now"),
            ("POST", f"/graph-drafts/{_ID}/accept-all"),
            ("POST", f"/graph-drafts/{_ID}/commit"),
        ):
            assert not service_principal_can_access(
                method, path, read_only=read_only, role=role, scope=PAT_SCOPE_GRAPH_CURATE
            ), (read_only, role, method, path)


def test_graph_curate_token_stages_notes_only_on_the_direct_write_routes(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    token = _token_headers(client, admin_auth_headers, scope=PAT_SCOPE_GRAPH_CURATE)
    staged = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "bench note", "status": "staged"},
        headers=token,
    )
    assert staged.status_code == 201, staged.text
    committed = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "bench note", "status": "committed"},
        headers=token,
    )
    assert committed.status_code == 403, committed.text
    assert committed.json()["error"]["code"] == "service_forbidden"


# --- granting --------------------------------------------------------------------


def test_widening_delegated_curation_needs_the_acknowledgement_in_the_same_request(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    path = _settings_path(project_id)

    refused = client.patch(
        path, json={"delegated_curation": "organize"}, headers=admin_auth_headers
    )
    assert refused.status_code == 422, refused.text
    assert DELEGATED_CURATION_ACKNOWLEDGEMENT_REQUIRED in refused.json()["error"]["message"]
    current = client.get(path, headers=admin_auth_headers).json()["data"]
    assert current["delegated_curation"] == "off"
    assert current["delegated_curation_granted_at"] is None

    granted = _grant(client, admin_auth_headers, project_id, "organize")
    assert granted["delegated_curation"] == "organize"
    assert granted["delegated_curation_granted_at"] is not None
    assert granted["delegated_curation_granted_by"] == granted["updated_by"]

    # Each widening is a fresh act of consent, never inherited from the last one.
    widened = client.patch(path, json={"delegated_curation": "full"}, headers=admin_auth_headers)
    assert widened.status_code == 422, widened.text
    assert _grant(client, admin_auth_headers, project_id, "full")["delegated_curation"] == "full"


def test_narrowing_delegated_curation_needs_no_acknowledgement_and_clears_the_grant(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    _grant(client, admin_auth_headers, project_id, "full")

    narrowed = client.patch(
        _settings_path(project_id), json={"delegated_curation": "off"}, headers=admin_auth_headers
    )
    assert narrowed.status_code == 200, narrowed.text
    data = narrowed.json()["data"]
    assert data["delegated_curation"] == "off"
    assert data["delegated_curation_granted_at"] is None
    assert data["delegated_curation_granted_by"] is None
    # Repeating the current value is a no-op, not a consent event.
    again = client.patch(
        _settings_path(project_id), json={"delegated_curation": "off"}, headers=admin_auth_headers
    )
    assert again.status_code == 200, again.text
    assert again.json()["data"]["updated_at"] == data["updated_at"]


def test_personal_settings_rows_never_carry_a_grant(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    personal = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings",
        json={"delegated_curation": "organize", "delegated_curation_acknowledged": True},
        headers=admin_auth_headers,
    )
    assert personal.status_code == 422, personal.text
    assert DELEGATED_CURATION_PROJECT_LEVEL_ONLY in personal.json()["error"]["message"]
    # The personal row exists (it inherits the project default) and stays off.
    personal_row = client.get(
        f"/projects/{project_id}/graph-draft-batch-settings", headers=admin_auth_headers
    ).json()["data"]
    assert personal_row["delegated_curation"] == "off"


def test_personal_route_refuses_the_grant_even_with_auth_disabled(
    monkeypatch, migrated_sqlite_database_url: str
) -> None:
    # An auth-disabled host routes the personal endpoint to the project-default
    # row, so the refusal is the route's, not the row's.
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "false")
    with TestClient(create_app()) as local_client:
        project_id = local_client.post("/projects", json={"name": "Local"}).json()["data"][
            "project_id"
        ]
        refused = local_client.patch(
            f"/projects/{project_id}/graph-draft-batch-settings",
            json={"delegated_curation": "full", "delegated_curation_acknowledged": True},
        )
        assert refused.status_code == 422, refused.text
        assert DELEGATED_CURATION_PROJECT_LEVEL_ONLY in refused.json()["error"]["message"]
        current = local_client.get(_settings_path(project_id)).json()["data"]
        assert current["delegated_curation"] == "off"
        # The project-default endpoint still grants, as the owner's act.
        granted = local_client.patch(
            _settings_path(project_id),
            json={"delegated_curation": "organize", "delegated_curation_acknowledged": True},
        )
        assert granted.status_code == 200, granted.text
        assert granted.json()["data"]["delegated_curation"] == "organize"


def test_grant_patch_rejects_malformed_consent_bodies(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    for body in (
        {"delegated_curation_acknowledged": True},
        {"delegated_curation": None},
        {"delegated_curation_acknowledged": None},
        {"delegated_curation_acknowledged": False},
        {"delegated_curation": "everything"},
        {"delegated_curation": "organize", "delegated_curation_acknowledged": False},
    ):
        response = client.patch(_settings_path(project_id), json=body, headers=admin_auth_headers)
        assert response.status_code == 422, (body, response.text)


def test_granting_is_an_owners_interactive_act(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    contributor_headers, contributor_id = _registered_user(client, role=Role.EDITOR)
    membership = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": contributor_id, "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert membership.status_code in {200, 201}, membership.text
    refused = client.patch(
        _settings_path(project_id),
        json={"delegated_curation": "organize", "delegated_curation_acknowledged": True},
        headers=contributor_headers,
    )
    assert refused.status_code == 403, refused.text

    with _request_api(client) as api, pytest.raises(
        PermissionDeniedError, match="interactive human session"
    ):
        api.update_graph_draft_batch_settings(
            UUID(project_id),
            delegated_curation=DelegatedCurationPolicy.ORGANIZE,
            delegated_curation_acknowledged=True,
            actor=_service_actor(PAT_SCOPE_ALL),
        )
    with _request_api(client) as api, pytest.raises(
        PermissionDeniedError, match="interactive human session"
    ):
        api.update_graph_draft_batch_settings(
            UUID(project_id),
            delegated_curation=DelegatedCurationPolicy.ORGANIZE,
            delegated_curation_acknowledged=True,
            actor=system_auth_context(),
        )
    assert (
        client.get(_settings_path(project_id), headers=admin_auth_headers).json()["data"][
            "delegated_curation"
        ]
        == "off"
    )


# --- the review gate for a graph_curate token -----------------------------------------


def test_graph_curate_token_is_refused_until_the_owner_delegates(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    _project_id, note_id, question_id = _linked_setup(client, admin_auth_headers)
    draft = _note_draft(client, admin_auth_headers, note_id, _patch(_link_op(note_id, question_id)))
    change_set_id = draft["change_set_id"]
    token = _token_headers(client, admin_auth_headers, scope=PAT_SCOPE_GRAPH_CURATE)

    assert client.get(f"/graph-drafts/{change_set_id}", headers=token).status_code == 200
    accept_all = client.post(f"/graph-drafts/{change_set_id}/accept-all", headers=token)
    assert accept_all.status_code == 403, accept_all.text
    assert "Delegated curation is off" in accept_all.json()["error"]["message"]
    operation_id = draft["operations"][0]["operation_id"]
    single = client.patch(
        f"/graph-drafts/{change_set_id}/operations/{operation_id}",
        json={"status": "accepted"},
        headers=token,
    )
    assert single.status_code == 403, single.text
    commit = client.post(
        f"/graph-drafts/{change_set_id}/commit", json={"message": "agent"}, headers=token
    )
    assert commit.status_code == 403, commit.text
    assert _read_draft(client, admin_auth_headers, change_set_id)["status"] == "ready"


def test_all_scope_token_stays_refused_even_with_delegation_on(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id, note_id, question_id = _linked_setup(client, admin_auth_headers)
    # Draft first, then grant, so the drafting pass never touched this draft.
    draft = _note_draft(client, admin_auth_headers, note_id, _patch(_link_op(note_id, question_id)))
    change_set_id = draft["change_set_id"]
    _grant(client, admin_auth_headers, project_id, "full")
    for scope in (PAT_SCOPE_ALL, PAT_SCOPE_STAGE_EVIDENCE):
        token = _token_headers(client, admin_auth_headers, scope=scope, role="admin")
        accept_all = client.post(f"/graph-drafts/{change_set_id}/accept-all", headers=token)
        assert accept_all.status_code == 403, (scope, accept_all.text)
        commit = client.post(
            f"/graph-drafts/{change_set_id}/commit", json={"message": "agent"}, headers=token
        )
        assert commit.status_code == 403, (scope, commit.text)
        if scope == PAT_SCOPE_ALL:
            # The all-scope token reaches the service gate, which names the
            # credential to mint; the stage token is stopped by the middleware.
            assert "Curate graph (delegated)" in accept_all.json()["error"]["message"]
        else:
            assert accept_all.json()["error"]["code"] == "service_forbidden"
    assert _read_draft(client, admin_auth_headers, change_set_id)["status"] == "ready"


def test_organize_grant_pre_accepts_links_and_leaves_the_rest_for_a_person(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id, note_id, question_id = _linked_setup(client, admin_auth_headers)
    _grant(client, admin_auth_headers, project_id, "organize")
    draft = _note_draft(
        client,
        admin_auth_headers,
        note_id,
        _patch(_question_op(project_id), _link_op(note_id, question_id)),
    )
    change_set_id = draft["change_set_id"]
    # The drafting pass already pre-accepted the link under the grant; the
    # question stayed proposed. Reset the link so the token's own accept is observed.
    assert draft["status"] == "ready"
    token = _token_headers(client, admin_auth_headers, scope=PAT_SCOPE_GRAPH_CURATE)
    operations = _by_semantic(draft)
    reopened = client.patch(
        f"/graph-drafts/{change_set_id}/operations/{operations['link_note_to_question']['operation_id']}",
        json={"status": "proposed"},
        headers=admin_auth_headers,
    )
    assert reopened.status_code == 200, reopened.text

    accepted = client.post(f"/graph-drafts/{change_set_id}/accept-all", headers=token)
    assert accepted.status_code == 200, accepted.text
    operations = _by_semantic(accepted.json()["data"])
    link = operations["link_note_to_question"]
    assert link["status"] == "accepted"
    assert link["acceptance_mode"] == "auto_accepted"
    assert link["accepted_at"] is not None
    question = operations["suggest_new_question"]
    assert question["status"] == "proposed"
    assert question["acceptance_mode"] is None

    outside = client.patch(
        f"/graph-drafts/{change_set_id}/operations/{question['operation_id']}",
        json={"status": "accepted"},
        headers=token,
    )
    assert outside.status_code == 403, outside.text
    assert "does not admit suggest_new_question" in outside.json()["error"]["message"]

    commit = client.post(
        f"/graph-drafts/{change_set_id}/commit", json={"message": "agent"}, headers=token
    )
    assert commit.status_code == 403, commit.text
    assert "still need a person" in commit.json()["error"]["message"]

    # The person finishes the review: their own accept is human_selected, and the
    # delegated accept keeps its honest label through commit.
    human = client.patch(
        f"/graph-drafts/{change_set_id}/operations/{question['operation_id']}",
        json={"status": "accepted"},
        headers=admin_auth_headers,
    )
    assert human.status_code == 200, human.text
    committed = client.post(
        f"/graph-drafts/{change_set_id}/commit",
        json={"message": "person commit"},
        headers=admin_auth_headers,
    )
    assert committed.status_code == 200, committed.text
    operations = _by_semantic(committed.json()["data"])
    assert operations["link_note_to_question"]["acceptance_mode"] == "auto_accepted"
    assert operations["link_note_to_question"]["status"] == "applied"
    assert operations["suggest_new_question"]["acceptance_mode"] == "human_selected"


def test_full_grant_lets_a_curate_token_accept_and_commit(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id, note_id, question_id = _linked_setup(client, admin_auth_headers)
    # Draft first, then grant: the pass never touched this draft.
    draft = _note_draft(
        client,
        admin_auth_headers,
        note_id,
        _patch(_question_op(project_id), _link_op(note_id, question_id)),
    )
    change_set_id = draft["change_set_id"]
    assert all(op["acceptance_mode"] is None for op in draft["operations"])
    _grant(client, admin_auth_headers, project_id, "full")
    token = _token_headers(client, admin_auth_headers, scope=PAT_SCOPE_GRAPH_CURATE)

    accepted = client.post(f"/graph-drafts/{change_set_id}/accept-all", headers=token)
    assert accepted.status_code == 200, accepted.text
    assert {op["acceptance_mode"] for op in accepted.json()["data"]["operations"]} == {
        "auto_accepted"
    }
    committed = client.post(
        f"/graph-drafts/{change_set_id}/commit",
        json={"message": "agent commit"},
        headers=token,
    )
    assert committed.status_code == 200, committed.text
    data = committed.json()["data"]
    assert data["status"] == "committed"
    me = client.get("/auth/me", headers=admin_auth_headers).json()["data"]
    assert data["committed_by"] == me["user_id"]
    assert {op["status"] for op in data["operations"]} == {"applied"}
    note = client.get(f"/notes/{note_id}", headers=admin_auth_headers).json()["data"]
    assert question_id in {target["entity_id"] for target in note["targets"]}
    new_question_id = _by_semantic(data)["suggest_new_question"]["result_entity_id"]
    created = client.get(f"/questions/{new_question_id}", headers=admin_auth_headers)
    assert created.status_code == 200, created.text
    assert created.json()["data"]["origin"] == "ai_suggested"


def test_curate_token_commit_still_requires_project_owner(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id, note_id, question_id = _linked_setup(client, admin_auth_headers)
    contributor_headers, contributor_id = _registered_user(client, role=Role.EDITOR)
    membership = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": contributor_id, "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert membership.status_code in {200, 201}, membership.text
    draft = _note_draft(
        client, contributor_headers, note_id, _patch(_link_op(note_id, question_id))
    )
    change_set_id = draft["change_set_id"]
    _grant(client, admin_auth_headers, project_id, "organize")
    token = _token_headers(
        client, contributor_headers, scope=PAT_SCOPE_GRAPH_CURATE, role="editor"
    )

    accepted = client.post(f"/graph-drafts/{change_set_id}/accept-all", headers=token)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["operations"][0]["acceptance_mode"] == "auto_accepted"
    commit = client.post(
        f"/graph-drafts/{change_set_id}/commit", json={"message": "agent"}, headers=token
    )
    assert commit.status_code == 403, commit.text
    assert "owner" in commit.json()["error"]["message"].lower()


def test_delegated_commit_is_refused_when_an_accepted_proposal_left_the_grant(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id, note_id, question_id = _linked_setup(client, admin_auth_headers)
    draft = _note_draft(
        client,
        admin_auth_headers,
        note_id,
        _patch(_question_op(project_id), _link_op(note_id, question_id)),
    )
    change_set_id = draft["change_set_id"]
    _grant(client, admin_auth_headers, project_id, "full")
    token = _token_headers(client, admin_auth_headers, scope=PAT_SCOPE_GRAPH_CURATE)
    accepted = client.post(f"/graph-drafts/{change_set_id}/accept-all", headers=token)
    assert accepted.status_code == 200, accepted.text
    assert {op["status"] for op in accepted.json()["data"]["operations"]} == {"accepted"}

    # The owner narrows the grant before the commit: the question no longer fits.
    narrowed = client.patch(
        _settings_path(project_id),
        json={"delegated_curation": "organize", "delegated_curation_acknowledged": True},
        headers=admin_auth_headers,
    )
    assert narrowed.status_code == 200, narrowed.text
    commit = client.post(
        f"/graph-drafts/{change_set_id}/commit", json={"message": "agent"}, headers=token
    )
    assert commit.status_code == 403, commit.text
    assert "does not admit suggest_new_question" in commit.json()["error"]["message"]
    assert _read_draft(client, admin_auth_headers, change_set_id)["status"] == "ready"


def test_narrowing_between_grants_still_needs_the_acknowledgement(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    _grant(client, admin_auth_headers, project_id, "full")
    refused = client.patch(
        _settings_path(project_id),
        json={"delegated_curation": "organize"},
        headers=admin_auth_headers,
    )
    assert refused.status_code == 422, refused.text
    assert DELEGATED_CURATION_ACKNOWLEDGEMENT_REQUIRED in refused.json()["error"]["message"]
    granted = _grant(client, admin_auth_headers, project_id, "organize")
    assert granted["delegated_curation"] == "organize"
    assert granted["delegated_curation_granted_at"] is not None


def test_graph_curate_token_only_previews_evidence_bundles() -> None:
    from lab_tracker.routes.shared import ensure_scope_allows_evidence_bundle

    curate = _service_actor(PAT_SCOPE_GRAPH_CURATE)
    ensure_scope_allows_evidence_bundle(curate, dry_run=True)
    with pytest.raises(ServiceScopeDeniedError, match="dry_run=true"):
        ensure_scope_allows_evidence_bundle(curate, dry_run=False)


def test_drafting_pass_runs_once_even_if_a_person_reopens_its_accept(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id, note_id, question_id = _linked_setup(client, admin_auth_headers)
    _grant(client, admin_auth_headers, project_id, "organize")
    patch = _patch(_question_op(project_id), _link_op(note_id, question_id))
    draft = _note_draft(client, admin_auth_headers, note_id, patch)
    change_set_id = draft["change_set_id"]
    link = _by_semantic(draft)["link_note_to_question"]
    assert link["acceptance_mode"] == "auto_accepted"
    reopened = client.patch(
        f"/graph-drafts/{change_set_id}/operations/{link['operation_id']}",
        json={"status": "proposed"},
        headers=admin_auth_headers,
    )
    assert reopened.status_code == 200, reopened.text

    again = _note_draft(client, admin_auth_headers, note_id, patch)

    assert again["change_set_id"] == change_set_id
    link = _by_semantic(again)["link_note_to_question"]
    assert link["status"] == "proposed"
    assert link["acceptance_mode"] is None


def test_drafting_pass_runs_in_the_background_worker(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    question_id = _question(client, admin_auth_headers, project_id)
    note_id = _note(client, admin_auth_headers, project_id, "Gel photo A looked clean.")
    _grant(client, admin_auth_headers, project_id, "organize")
    client.app.state.settings.graph_draft_background_enabled = True
    fake_client = FakeBatchDraftClient(_patch(_link_op(note_id, question_id)))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client
    queued = client.post(
        "/batches/run-now", json={"project_id": project_id}, headers=admin_auth_headers
    )
    assert queued.status_code == 201, queued.text
    assert queued.json()["data"]["status"] == "pending"

    with client.app.state.db_session_factory() as session:
        api = LabTrackerAPI(
            raw_storage=client.app.state.raw_note_storage,
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=client.app.state.settings,
            surface="background",
        )
        run = api.process_next_graph_draft_batch_run(
            draft_client_factory=client.app.state.graph_draft_client_factory,
            app_settings=client.app.state.settings,
            actor=system_auth_context(),
        )
    assert run is not None and run.change_set_id is not None
    assert run.status.value == "ready"
    draft = _read_draft(client, admin_auth_headers, str(run.change_set_id))
    assert draft["status"] == "committed"
    assert draft["operations"][0]["acceptance_mode"] == "auto_accepted"
    assert draft["context_packet"][DELEGATED_CURATION_PACKET_KEY]["committed"] is True


def test_delegated_principals_may_only_accept(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id, note_id, question_id = _linked_setup(client, admin_auth_headers)
    draft = _note_draft(client, admin_auth_headers, note_id, _patch(_link_op(note_id, question_id)))
    change_set_id = draft["change_set_id"]
    operation = draft["operations"][0]
    _grant(client, admin_auth_headers, project_id, "full")
    token = _token_headers(client, admin_auth_headers, scope=PAT_SCOPE_GRAPH_CURATE)
    path = f"/graph-drafts/{change_set_id}/operations/{operation['operation_id']}"
    for body in (
        {"status": "rejected"},
        {"status": "proposed"},
        {"status": "accepted", "review_note": "looks right"},
        {"status": "accepted", "payload": operation["payload"]},
        {"deferred": True},
        {"review_note": "note only"},
    ):
        response = client.patch(path, json=body, headers=token)
        assert response.status_code == 403, (body, response.text)
        assert "may only accept" in response.json()["error"]["message"]
    accepted = client.patch(path, json={"status": "accepted"}, headers=token)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["operations"][0]["acceptance_mode"] == "auto_accepted"


def test_delegated_principals_never_override_a_persons_verdict(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id, note_id, question_id = _linked_setup(client, admin_auth_headers)
    draft = _note_draft(
        client,
        admin_auth_headers,
        note_id,
        _patch(_link_op(note_id, question_id), _question_op(project_id)),
    )
    change_set_id = draft["change_set_id"]
    operations = _by_semantic(draft)
    link_path = (
        f"/graph-drafts/{change_set_id}/operations/"
        f"{operations['link_note_to_question']['operation_id']}"
    )
    question_path = (
        f"/graph-drafts/{change_set_id}/operations/"
        f"{operations['suggest_new_question']['operation_id']}"
    )
    assert (
        client.patch(link_path, json={"status": "rejected"}, headers=admin_auth_headers).status_code
        == 200
    )
    assert (
        client.patch(question_path, json={"deferred": True}, headers=admin_auth_headers).status_code
        == 200
    )
    _grant(client, admin_auth_headers, project_id, "full")
    token = _token_headers(client, admin_auth_headers, scope=PAT_SCOPE_GRAPH_CURATE)

    for path in (link_path, question_path):
        refused = client.patch(path, json={"status": "accepted"}, headers=token)
        assert refused.status_code == 403, refused.text
        assert "verdict stands" in refused.json()["error"]["message"]
    # Accept-all skips both as well: one is decided, the other set aside.
    accepted = client.post(f"/graph-drafts/{change_set_id}/accept-all", headers=token)
    assert accepted.status_code == 200, accepted.text
    operations = _by_semantic(accepted.json()["data"])
    assert operations["link_note_to_question"]["status"] == "rejected"
    assert operations["suggest_new_question"]["status"] == "proposed"
    assert operations["suggest_new_question"]["acceptance_mode"] is None


def test_drafting_pass_never_touches_a_draft_a_person_has_started(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id, note_id, question_id = _linked_setup(client, admin_auth_headers)
    patch = _patch(_link_op(note_id, question_id), _question_op(project_id))
    draft = _note_draft(client, admin_auth_headers, note_id, patch)
    change_set_id = draft["change_set_id"]
    operations = _by_semantic(draft)
    rejected = client.patch(
        f"/graph-drafts/{change_set_id}/operations/"
        f"{operations['suggest_new_question']['operation_id']}",
        json={"status": "rejected"},
        headers=admin_auth_headers,
    )
    assert rejected.status_code == 200, rejected.text
    _grant(client, admin_auth_headers, project_id, "full")

    # Re-requesting the same draft returns the existing one; the pass leaves it alone.
    again = _note_draft(client, admin_auth_headers, note_id, patch)
    assert again["change_set_id"] == change_set_id
    assert again["status"] == "ready"
    assert DELEGATED_CURATION_PACKET_KEY not in again["context_packet"]
    operations = _by_semantic(again)
    assert operations["link_note_to_question"]["status"] == "proposed"
    assert operations["link_note_to_question"]["acceptance_mode"] is None
    assert operations["suggest_new_question"]["status"] == "rejected"


def test_drafting_pass_attributes_applied_records_to_the_granting_owner(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id, note_id, question_id = _linked_setup(client, admin_auth_headers)
    granted = _grant(client, admin_auth_headers, project_id, "full")

    draft = _note_draft(
        client,
        admin_auth_headers,
        note_id,
        _patch(_question_op(project_id), _link_op(note_id, question_id)),
    )

    assert draft["status"] == "committed"
    assert draft["committed_by"] == granted["delegated_curation_granted_by"]
    new_question_id = _by_semantic(draft)["suggest_new_question"]["result_entity_id"]
    created = client.get(f"/questions/{new_question_id}", headers=admin_auth_headers).json()[
        "data"
    ]
    assert created["origin"] == "ai_suggested"
    assert created["created_by"] == granted["delegated_curation_granted_by"]
    assert created["change_set_id"] == draft["change_set_id"]


def test_drafting_pass_is_one_unit_outside_a_request(
    client: TestClient, admin_auth_headers: dict[str, str], monkeypatch
) -> None:
    """In the background worker's context a failed commit takes its accepts with it."""
    project_id = _project(client, admin_auth_headers)
    question_id = _question(client, admin_auth_headers, project_id)
    note_id = _note(client, admin_auth_headers, project_id, "Gel photo A looked clean.")
    _grant(client, admin_auth_headers, project_id, "organize")
    me = client.get("/auth/me", headers=admin_auth_headers).json()["data"]["user_id"]

    def refuse(self, change_set_id, *, message, actor=None):
        raise ValidationError("the applier refused this one")

    monkeypatch.setattr(TransactionalDraftCommitCoordinator, "commit_graph_change_set", refuse)
    with client.app.state.db_session_factory() as session:
        api = LabTrackerAPI(
            raw_storage=client.app.state.raw_note_storage,
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=client.app.state.settings,
            surface="background",
        )
        run = api.run_graph_draft_batch_for_project(
            UUID(project_id),
            draft_client=FakeBatchDraftClient(_patch(_link_op(note_id, question_id))),
            actor=AuthContext(user_id=UUID(me), role=Role.ADMIN),
        )
        assert run.change_set_id is not None

    draft = _read_draft(client, admin_auth_headers, str(run.change_set_id))
    assert draft["status"] == "ready"
    assert draft["error_metadata"][DELEGATED_CURATION_ERROR_KEY]["message"] == (
        "the applier refused this one"
    )
    assert draft["operations"][0]["status"] == "proposed"
    assert draft["operations"][0]["acceptance_mode"] is None
    assert DELEGATED_CURATION_PACKET_KEY not in draft["context_packet"]


def test_review_email_is_dropped_for_a_draft_the_pass_committed(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    client.app.state.settings.review_email_enabled = True
    project_id = _project(client, admin_auth_headers)
    question_id = _question(client, admin_auth_headers, project_id)
    note_id = _note(client, admin_auth_headers, project_id, "Gel photo A looked clean.")
    me = client.get("/auth/me", headers=admin_auth_headers).json()["data"]["user_id"]
    configured = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings",
        json={
            "enabled": True,
            "email_notifications_enabled": True,
            "notification_email": "reviewer@example.org",
        },
        headers=admin_auth_headers,
    )
    assert configured.status_code == 200, configured.text
    _grant(client, admin_auth_headers, project_id, "organize")

    with client.app.state.db_session_factory() as session:
        api = LabTrackerAPI(
            raw_storage=client.app.state.raw_note_storage,
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=client.app.state.settings,
        )
        run = api.run_graph_draft_batch_for_project(
            UUID(project_id),
            draft_client=FakeBatchDraftClient(_patch(_link_op(note_id, question_id))),
            actor=AuthContext(user_id=UUID(me), role=Role.ADMIN),
            review_assignee=me,
            review_assignee_user_id=UUID(me),
        )
        assert run.change_set_id is not None
        assert api.get_graph_change_set(run.change_set_id).status is GraphChangeSetStatus.COMMITTED
        [queued] = api.review_emails.list()
        assert queued.change_set_id == run.change_set_id
        assert queued.status is ReviewEmailDeliveryStatus.PENDING

        # Nothing is left to review, so the cue is cancelled at send time, not sent.
        assert api.review_emails.claim_next(lease_seconds=60) is None
        [dropped] = api.review_emails.list()
        assert dropped.status is ReviewEmailDeliveryStatus.FAILED
        assert "no longer waiting" in (dropped.last_error or "")


def test_a_person_never_records_auto_accepted(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id, note_id, _question_id = _linked_setup(client, admin_auth_headers)
    draft = _note_draft(client, admin_auth_headers, note_id, _patch(_question_op(project_id)))
    operation = draft["operations"][0]
    with _request_api(client) as api, pytest.raises(ValidationError, match="auto_accepted"):
        api.update_graph_change_operation(
            UUID(draft["change_set_id"]),
            UUID(operation["operation_id"]),
            status=GraphChangeOperationStatus.ACCEPTED,
            acceptance_mode=AcceptanceMode.AUTO_ACCEPTED,
            actor=AuthContext(user_id=uuid4(), role=Role.ADMIN),
        )


def test_repository_persists_auto_accepted() -> None:
    operation = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=0,
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.NOTE,
        semantic_type=GraphDraftSemanticType.LINK_NOTE_TO_QUESTION,
        status=GraphChangeOperationStatus.ACCEPTED,
        acceptance_mode=AcceptanceMode.AUTO_ACCEPTED,
    )
    assert operation_to_model(operation).acceptance_mode == "auto_accepted"


# --- the system principal ------------------------------------------------------------


def test_system_actor_acts_only_within_the_grant(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id, note_id, question_id = _linked_setup(client, admin_auth_headers)
    draft = _note_draft(
        client,
        admin_auth_headers,
        note_id,
        _patch(_question_op(project_id), _link_op(note_id, question_id)),
    )
    change_set_id = UUID(draft["change_set_id"])
    with _request_api(client) as api, pytest.raises(PermissionDeniedError):
        api.bulk_accept_graph_change_operations(change_set_id, actor=system_auth_context())

    _grant(client, admin_auth_headers, project_id, "organize")
    with _request_api(client) as api:
        change_set = api.bulk_accept_graph_change_operations(
            change_set_id, actor=system_auth_context()
        )
        by_type = {op.semantic_type: op for op in change_set.operations}
        assert by_type[GraphDraftSemanticType.LINK_NOTE_TO_QUESTION].acceptance_mode == (
            AcceptanceMode.AUTO_ACCEPTED
        )
        assert by_type[GraphDraftSemanticType.SUGGEST_NEW_QUESTION].status == (
            GraphChangeOperationStatus.PROPOSED
        )
        with pytest.raises(PermissionDeniedError, match="still need a person"):
            api.commit_graph_change_set(change_set_id, message="auto", actor=system_auth_context())

    _grant(client, admin_auth_headers, project_id, "full")
    with _request_api(client) as api:
        api.bulk_accept_graph_change_operations(change_set_id, actor=system_auth_context())
        committed = api.commit_graph_change_set(
            change_set_id, message="auto", actor=system_auth_context()
        )
        assert committed.status == GraphChangeSetStatus.COMMITTED
        assert {op.acceptance_mode for op in committed.operations} == {
            AcceptanceMode.AUTO_ACCEPTED
        }


def test_member_onboarding_drafts_are_never_delegated(client: TestClient) -> None:
    change_set = GraphChangeSet(
        change_set_id=uuid4(),
        project_id=uuid4(),
        source_note_id=uuid4(),
        model="fake",
        prompt_version="v1",
        purpose=GraphDraftPurpose.MEMBER_CHECKPOINT_ALIGNMENT,
        status=GraphChangeSetStatus.READY,
        operations=[_operation(GraphDraftSemanticType.LINK_NOTE_TO_QUESTION)],
    )
    with _request_api(client) as api:
        authorization = api.project_authorization
        with pytest.raises(PermissionDeniedError, match="never delegated"):
            authorization.require_delegated_grant(
                system_auth_context(), change_set=change_set, action="Accepting graph operations"
            )
        with pytest.raises(ValueError):
            authorization.require_delegated_grant(
                AuthContext(user_id=uuid4(), role=Role.ADMIN),
                change_set=change_set,
                action="Accepting graph operations",
            )
        assert api.apply_delegated_curation(change_set) is change_set


# --- the drafting pass ---------------------------------------------------------------


def _run_now(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    fake_client = FakeBatchDraftClient(patch)
    client.app.state.graph_draft_client_factory = lambda settings: fake_client
    response = client.post("/batches/run-now", json={"project_id": project_id}, headers=headers)
    assert response.status_code == 201, response.text
    assert fake_client.closed is True
    return response.json()["data"]


def test_drafting_pass_commits_a_fully_organizational_batch(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    question_id = _question(client, admin_auth_headers, project_id)
    note_id = _note(client, admin_auth_headers, project_id, "Gel photo A looked clean.")
    granted = _grant(client, admin_auth_headers, project_id, "organize")

    run = _run_now(client, admin_auth_headers, project_id, _patch(_link_op(note_id, question_id)))

    assert run["status"] == "ready"
    draft = _read_draft(client, admin_auth_headers, run["change_set_id"])
    assert draft["status"] == "committed"
    assert draft["commit_message"].startswith("Delegated curation (organize): applied 1 proposal")
    # The pass acts under the owner's grant, so the owner is the person of record
    # and the auto_accepted label is what says nobody looked.
    assert draft["committed_by"] == granted["delegated_curation_granted_by"]
    operation = draft["operations"][0]
    assert operation["status"] == "applied"
    assert operation["acceptance_mode"] == "auto_accepted"
    assert operation["accepted_by"] == granted["delegated_curation_granted_by"]
    assert operation["accepted_by_user_id"] == granted["delegated_curation_granted_by"]
    packet = draft["context_packet"][DELEGATED_CURATION_PACKET_KEY]
    assert packet["policy"] == "organize"
    assert packet["granted_by"] == granted["delegated_curation_granted_by"]
    assert packet["committed"] is True
    assert packet["left_for_review"] == 0
    assert packet["accepted_operation_ids"] == [operation["operation_id"]]
    note = client.get(f"/notes/{note_id}", headers=admin_auth_headers).json()["data"]
    assert question_id in {target["entity_id"] for target in note["targets"]}
    # Nothing is left in the person's queue for this batch.
    queue = client.get(
        f"/batches?project_id={project_id}&mine=true&status=ready", headers=admin_auth_headers
    ).json()["data"]
    assert [item["change_set_id"] for item in queue] == []


def test_drafting_pass_pre_accepts_links_in_a_mixed_batch_and_leaves_it_for_review(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    question_id = _question(client, admin_auth_headers, project_id)
    note_id = _note(client, admin_auth_headers, project_id, "Gel photo A looked clean.")
    _grant(client, admin_auth_headers, project_id, "organize")

    run = _run_now(
        client,
        admin_auth_headers,
        project_id,
        _patch(_question_op(project_id), _link_op(note_id, question_id)),
    )

    assert run["status"] == "ready"
    draft = _read_draft(client, admin_auth_headers, run["change_set_id"])
    assert draft["status"] == "ready"
    operations = _by_semantic(draft)
    assert operations["link_note_to_question"]["status"] == "accepted"
    assert operations["link_note_to_question"]["acceptance_mode"] == "auto_accepted"
    assert operations["suggest_new_question"]["status"] == "proposed"
    packet = draft["context_packet"][DELEGATED_CURATION_PACKET_KEY]
    assert packet["committed"] is False
    assert packet["left_for_review"] == 1
    queue = client.get(
        f"/batches?project_id={project_id}&mine=true&status=ready", headers=admin_auth_headers
    ).json()["data"]
    assert [item["change_set_id"] for item in queue] == [run["change_set_id"]]


def test_drafting_pass_applies_full_grant_to_note_scoped_drafts(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id, note_id, question_id = _linked_setup(client, admin_auth_headers)
    _grant(client, admin_auth_headers, project_id, "full")

    draft = _note_draft(
        client,
        admin_auth_headers,
        note_id,
        _patch(_question_op(project_id), _link_op(note_id, question_id)),
    )

    assert draft["status"] == "committed"
    assert {op["acceptance_mode"] for op in draft["operations"]} == {"auto_accepted"}
    assert {op["status"] for op in draft["operations"]} == {"applied"}
    assert draft["context_packet"][DELEGATED_CURATION_PACKET_KEY]["committed"] is True


def test_drafting_pass_leaves_clarification_requests_for_a_person(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id, note_id, question_id = _linked_setup(client, admin_auth_headers)
    _grant(client, admin_auth_headers, project_id, "full")
    clarification = {
        "client_ref": "ask1",
        "op": "create",
        "entity_type": "note",
        "semantic_type": "request_clarification",
        "target_entity_id": None,
        "payload_json": json.dumps(
            {
                "project_id": project_id,
                "raw_content": "Which rig was this?",
                "targets": [{"entity_type": "question", "entity_id": question_id}],
            }
        ),
        "rationale": "The capture names no rig.",
        "confidence": 0.5,
        "source_refs": [],
    }

    draft = _note_draft(
        client, admin_auth_headers, note_id, _patch(_link_op(note_id, question_id), clarification)
    )

    assert draft["status"] == "ready"
    operations = _by_semantic(draft)
    assert operations["link_note_to_question"]["acceptance_mode"] == "auto_accepted"
    assert operations["request_clarification"]["status"] == "proposed"


def test_drafting_pass_does_nothing_when_delegation_is_off(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    question_id = _question(client, admin_auth_headers, project_id)
    note_id = _note(client, admin_auth_headers, project_id, "Gel photo A looked clean.")

    run = _run_now(client, admin_auth_headers, project_id, _patch(_link_op(note_id, question_id)))

    draft = _read_draft(client, admin_auth_headers, run["change_set_id"])
    assert draft["status"] == "ready"
    assert draft["operations"][0]["status"] == "proposed"
    assert DELEGATED_CURATION_PACKET_KEY not in draft["context_packet"]


def test_drafting_pass_records_a_failed_commit_and_leaves_the_draft_for_a_person(
    client: TestClient, admin_auth_headers: dict[str, str], monkeypatch
) -> None:
    project_id = _project(client, admin_auth_headers)
    question_id = _question(client, admin_auth_headers, project_id)
    note_id = _note(client, admin_auth_headers, project_id, "Gel photo A looked clean.")
    _grant(client, admin_auth_headers, project_id, "organize")

    def refuse(self, change_set_id, *, message, actor=None):
        raise ValidationError("the applier refused this one")

    monkeypatch.setattr(TransactionalDraftCommitCoordinator, "commit_graph_change_set", refuse)

    run = _run_now(client, admin_auth_headers, project_id, _patch(_link_op(note_id, question_id)))

    assert run["status"] == "ready"
    draft = _read_draft(client, admin_auth_headers, run["change_set_id"])
    assert draft["status"] == "ready"
    failure = draft["error_metadata"][DELEGATED_CURATION_ERROR_KEY]
    assert failure["message"] == "the applier refused this one"
    assert failure["policy"] == "organize"
    # The pass is one unit: its accept is rolled back with the failed commit,
    # so the person reviews the proposal exactly as the model left it.
    assert draft["operations"][0]["status"] == "proposed"
    assert draft["operations"][0]["acceptance_mode"] is None
    monkeypatch.undo()
    committed = client.post(
        f"/graph-drafts/{run['change_set_id']}/accept-all", headers=admin_auth_headers
    )
    assert committed.status_code == 200, committed.text
    committed = client.post(
        f"/graph-drafts/{run['change_set_id']}/commit",
        json={"message": "person"},
        headers=admin_auth_headers,
    )
    assert committed.status_code == 200, committed.text
    # The failed pass stays on the record: the person, not the pass, applied this draft.
    assert committed.json()["data"]["error_metadata"][DELEGATED_CURATION_ERROR_KEY] == failure


# --- ledgers and schema ------------------------------------------------------------------


def test_draft_quality_counts_auto_accepts_separately() -> None:
    change_set_id = uuid4()
    rows = [
        _row(
            change_set_id=change_set_id,
            change_set_status=GraphChangeSetStatus.COMMITTED,
            operation_status=GraphChangeOperationStatus.APPLIED,
            acceptance_mode=AcceptanceMode.AUTO_ACCEPTED,
            accepted_at=T0 + timedelta(seconds=1),
        ),
        _row(
            change_set_id=change_set_id,
            change_set_status=GraphChangeSetStatus.COMMITTED,
            operation_status=GraphChangeOperationStatus.APPLIED,
            acceptance_mode=AcceptanceMode.HUMAN_SELECTED,
            accepted_at=T0 + timedelta(seconds=2),
        ),
    ]
    ledger = aggregate_draft_quality(uuid4(), None, rows)
    (cell,) = ledger.cells
    assert cell.accepted_total == 2
    assert cell.accepted_auto_accepted == 1
    assert cell.accepted_human_selected == 1
    assert cell.accepted_bulk_accepted == 0


def _alembic_config() -> Config:
    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


def _columns(database_url: str, table_name: str) -> set[str]:
    engine = create_engine(database_url, future=True)
    try:
        return {column["name"] for column in inspect(engine).get_columns(table_name)}
    finally:
        engine.dispose()


def test_delegated_curation_revision_extends_the_single_chain() -> None:
    script = ScriptDirectory.from_config(_alembic_config())
    assert script.get_revision(_MIGRATION).down_revision == _MIGRATION_PREVIOUS
    assert script.get_heads() == [_MIGRATION]


def test_delegated_curation_migration_defaults_existing_rows_to_off(monkeypatch, tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'delegation.db'}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _alembic_config()
    command.upgrade(config, _MIGRATION_PREVIOUS)
    assert not _MIGRATION_COLUMNS & _columns(database_url, "graph_draft_batch_settings")
    engine = create_engine(database_url, future=True)
    project_id = "00000000-0000-4000-8000-0000000000cc"
    settings_id = "00000000-0000-4000-8000-0000000000dd"
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projects (project_id, name, status, created_at, updated_at, "
                    "created_by) VALUES (:project_id, 'Existing', 'active', CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP, 'existing')"
                ),
                {"project_id": project_id},
            )
            connection.execute(
                text(
                    "INSERT INTO graph_draft_batch_settings (settings_id, project_id, enabled, "
                    "cadence_minutes, run_at_local_time, timezone_name, "
                    "email_notifications_enabled, external_context_policy, created_at, "
                    "updated_at) VALUES (:settings_id, :project_id, TRUE, 1440, '06:00', 'UTC', "
                    "FALSE, 'own_notes_only', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"settings_id": settings_id, "project_id": project_id},
            )
    finally:
        engine.dispose()

    command.upgrade(config, _MIGRATION)

    assert _columns(database_url, "graph_draft_batch_settings") >= _MIGRATION_COLUMNS
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT delegated_curation, delegated_curation_granted_at, "
                    "delegated_curation_granted_by FROM graph_draft_batch_settings "
                    "WHERE settings_id = :settings_id"
                ),
                {"settings_id": settings_id},
            ).one()
    finally:
        engine.dispose()
    assert tuple(row) == ("off", None, None)

    command.downgrade(config, _MIGRATION_PREVIOUS)

    assert not _MIGRATION_COLUMNS & _columns(database_url, "graph_draft_batch_settings")
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT count(*) FROM graph_draft_batch_settings")) == 1
            )
    finally:
        engine.dispose()
