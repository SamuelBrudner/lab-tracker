from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from lab_tracker.app import create_app
from lab_tracker.auth import LOCAL_AUTH_USER_ID, Role
from lab_tracker.db_models import NoteModel, ProjectModel, QuestionModel
from lab_tracker.services.note_service import NoteService
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.question_service import QuestionService


def _admin_headers(client: TestClient, prefix: str) -> dict[str, str]:
    username = f"{prefix}-{uuid4().hex[:8]}"
    password = "secret"
    client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=Role.ADMIN,
    )
    response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def test_project_capture_key_is_principal_scoped_and_conflict_honest(
    client: TestClient,
) -> None:
    first_headers = _admin_headers(client, "capture-a")
    second_headers = _admin_headers(client, "capture-b")
    payload = {
        "name": "Principal-scoped capture",
        "description": "same intent",
        "client_capture_id": "shared-project-key",
    }

    created = client.post("/projects", json=payload, headers=first_headers)
    reused = client.post("/projects", json=payload, headers=first_headers)
    conflicting = client.post(
        "/projects",
        json={**payload, "description": "different intent"},
        headers=first_headers,
    )
    other_principal = client.post("/projects", json=payload, headers=second_headers)

    assert created.status_code == 201, created.text
    assert reused.status_code == 200, reused.text
    assert reused.json()["data"]["project_id"] == created.json()["data"]["project_id"]
    assert conflicting.status_code == 409, conflicting.text
    assert conflicting.json()["error"]["code"] == "conflict"
    assert other_principal.status_code == 201, other_principal.text
    assert other_principal.json()["data"]["project_id"] != created.json()["data"]["project_id"]

    with client.app.state.db_session_factory() as session:
        rows = list(
            session.scalars(
                select(ProjectModel).where(
                    ProjectModel.client_capture_id == "shared-project-key"
                )
            )
        )
    assert len(rows) == 2
    assert len({row.created_by for row in rows}) == 2


def test_question_and_note_capture_keys_return_201_200_and_409(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project = client.post(
        "/projects",
        json={"name": "Nested capture keys"},
        headers=admin_auth_headers,
    ).json()["data"]
    project_id = project["project_id"]
    question_payload = {
        "project_id": project_id,
        "text": "Does the key preserve intent?",
        "question_type": "descriptive",
        "client_capture_id": "question-key",
    }
    note_payload = {
        "project_id": project_id,
        "raw_content": "Stable note content",
        "metadata": {"source": "capture-test"},
        "client_capture_id": "note-key",
    }

    question_created = client.post(
        "/questions", json=question_payload, headers=admin_auth_headers
    )
    question_reused = client.post(
        "/questions", json=question_payload, headers=admin_auth_headers
    )
    question_conflict = client.post(
        "/questions",
        json={**question_payload, "text": "Different question intent?"},
        headers=admin_auth_headers,
    )
    note_created = client.post("/notes", json=note_payload, headers=admin_auth_headers)
    note_reused = client.post("/notes", json=note_payload, headers=admin_auth_headers)
    note_conflict = client.post(
        "/notes",
        json={**note_payload, "raw_content": "Different note intent"},
        headers=admin_auth_headers,
    )

    assert question_created.status_code == 201, question_created.text
    assert question_reused.status_code == 200, question_reused.text
    assert question_reused.json()["data"]["question_id"] == question_created.json()["data"][
        "question_id"
    ]
    assert question_conflict.status_code == 409, question_conflict.text
    assert note_created.status_code == 201, note_created.text
    assert note_reused.status_code == 200, note_reused.text
    assert note_reused.json()["data"]["note_id"] == note_created.json()["data"]["note_id"]
    assert note_conflict.status_code == 409, note_conflict.text


def test_auth_disabled_mode_reuses_the_local_principal_key_deterministically(
    migrated_sqlite_database_url: str,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", migrated_sqlite_database_url)
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "false")
    payload = {
        "name": "Local deterministic capture",
        "client_capture_id": "local-project-key",
    }
    application = create_app()
    try:
        with TestClient(application) as local_client:
            created = local_client.post("/projects", json=payload)
    finally:
        application.state.db_engine.dispose()

    restarted_application = create_app()
    try:
        with TestClient(restarted_application) as local_client:
            reused = local_client.post("/projects", json=payload)
        with restarted_application.state.db_session_factory() as session:
            stored = session.scalar(
                select(ProjectModel).where(
                    ProjectModel.client_capture_id == "local-project-key"
                )
            )
    finally:
        restarted_application.state.db_engine.dispose()

    assert created.status_code == 201, created.text
    assert reused.status_code == 200, reused.text
    assert reused.json()["data"]["project_id"] == created.json()["data"]["project_id"]
    assert stored is not None
    assert stored.created_by == str(LOCAL_AUTH_USER_ID)


def test_project_integrity_error_recovers_the_existing_winner(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    payload = {
        "name": "Project race recovery",
        "client_capture_id": "project-race-key",
    }
    created = client.post("/projects", json=payload, headers=admin_auth_headers)
    assert created.status_code == 201, created.text

    original = ProjectService._find_client_capture_project
    calls = 0

    def miss_once(self, client_capture_id, *, created_by):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return original(self, client_capture_id, created_by=created_by)

    monkeypatch.setattr(ProjectService, "_find_client_capture_project", miss_once)
    replay = client.post("/projects", json=payload, headers=admin_auth_headers)

    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["project_id"] == created.json()["data"]["project_id"]
    assert calls == 2


def test_question_integrity_error_recovers_the_existing_winner(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    project_id = client.post(
        "/projects",
        json={"name": "Question race recovery"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    payload = {
        "project_id": project_id,
        "text": "Which question wins?",
        "question_type": "descriptive",
        "client_capture_id": "question-race-key",
    }
    created = client.post("/questions", json=payload, headers=admin_auth_headers)
    assert created.status_code == 201, created.text

    original = QuestionService._find_client_capture_question
    calls = 0

    def miss_once(self, candidate_project_id, client_capture_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return original(self, candidate_project_id, client_capture_id)

    monkeypatch.setattr(QuestionService, "_find_client_capture_question", miss_once)
    replay = client.post("/questions", json=payload, headers=admin_auth_headers)

    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["question_id"] == created.json()["data"]["question_id"]
    assert calls == 2
    with client.app.state.db_session_factory() as session:
        count = session.scalar(
            select(func.count()).select_from(QuestionModel).where(
                QuestionModel.project_id == project_id,
                QuestionModel.client_capture_id == "question-race-key",
            )
        )
    assert count == 1


def test_note_integrity_error_recovers_the_existing_winner(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    project_id = client.post(
        "/projects",
        json={"name": "Note race recovery"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    payload = {
        "project_id": project_id,
        "raw_content": "The winning note",
        "client_capture_id": "note-race-key",
    }
    created = client.post("/notes", json=payload, headers=admin_auth_headers)
    assert created.status_code == 201, created.text

    original = NoteService._find_client_capture_note
    calls = 0

    def miss_once(self, candidate_project_id, client_capture_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return original(self, candidate_project_id, client_capture_id)

    monkeypatch.setattr(NoteService, "_find_client_capture_note", miss_once)
    replay = client.post("/notes", json=payload, headers=admin_auth_headers)

    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["note_id"] == created.json()["data"]["note_id"]
    assert calls == 2
    with client.app.state.db_session_factory() as session:
        count = session.scalar(
            select(func.count()).select_from(NoteModel).where(
                NoteModel.project_id == project_id,
                NoteModel.client_capture_id == "note-race-key",
            )
        )
    assert count == 1
