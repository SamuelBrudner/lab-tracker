from uuid import UUID

import pytest
from test_session_routes import _create_operational_session, _create_project, _create_question

from lab_tracker.app_parts.runtime import _background_api
from lab_tracker.auth import AuthContext, PrincipalType, Role
from lab_tracker.errors import PermissionDeniedError
from lab_tracker.models import EntityOrigin, SessionType
from lab_tracker.work_intent import WorkIntentChoice


def test_scientist_declares_a_session_purpose_separate_from_links(
    client,
    admin_auth_headers,
    viewer_user,
):
    project = _create_project(client, admin_auth_headers, "Bench work")
    session = _create_operational_session(client, admin_auth_headers, project)
    endpoint = "/work-intent/session/" + session["session_id"]
    assert client.get(endpoint, headers=admin_auth_headers).json()["data"] == {"intent": None}
    assert client.get(endpoint, headers=viewer_user.headers).status_code == 404
    value = client.put(endpoint, headers=admin_auth_headers, json={"kind": "calibration"})
    assert value.status_code == 200, value.text
    intent = value.json()["data"]
    assert intent["kind"] == "calibration"
    assert intent["source"] == "declared"
    assert intent["recorded_by_user_id"] == session["created_by_user_id"]
    assert intent["question_id"] is None
    assert client.get(endpoint, headers=admin_auth_headers).json()["data"]["intent"] == intent
    other_project = _create_project(client, admin_auth_headers, "Different project")
    wrong_question = _create_question(client, admin_auth_headers, other_project)
    rejected = client.put(
        endpoint,
        headers=admin_auth_headers,
        json={"kind": "question", "question_id": wrong_question},
    )
    assert rejected.status_code == 422


def test_choosing_a_primary_question_during_capture_records_a_human_declaration(
    client,
    admin_auth_headers,
):
    project = _create_project(client, admin_auth_headers, "Purpose at capture")
    question_id = _create_question(client, admin_auth_headers, project)
    response = client.post(
        "/sessions",
        headers=admin_auth_headers,
        json={
            "project_id": project,
            "session_type": "scientific",
            "primary_question_id": question_id,
        },
    )
    assert response.status_code == 201, response.text
    session = response.json()["data"]
    read = client.get("/work-intent/session/" + session["session_id"], headers=admin_auth_headers)
    intent = read.json()["data"]["intent"]
    assert intent["question_id"] == question_id
    assert intent["source"] == "declared"
    assert intent["recorded_by_user_id"] == session["created_by_user_id"]
    actor = AuthContext(UUID(session["created_by_user_id"]), Role.ADMIN)
    with client.app.state.db_session_factory() as db:
        api = _background_api(client.app, db)
        proposal = api.create_session(
            UUID(project),
            SessionType.SCIENTIFIC,
            primary_question_id=UUID(question_id),
            origin=EntityOrigin.AI_SUGGESTED,
            actor=actor,
        )
        # The related question is present but carries no human intent.
        assert proposal.primary_question_id == UUID(question_id)
        assert api.work_intents.get("session", proposal.session_id, actor=actor) is None
        automated = AuthContext(actor.user_id, Role.ADMIN, principal_type=PrincipalType.SYSTEM)
        with pytest.raises(PermissionDeniedError):
            api.work_intents.record(
                "session",
                proposal.session_id,
                WorkIntentChoice(kind="exploratory"),
                actor=automated,
            )
