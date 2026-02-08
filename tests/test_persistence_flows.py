from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from lab_tracker.api import LabTrackerAPI
from lab_tracker.app import create_app
from lab_tracker.auth import AuthContext, Role
from lab_tracker.db import Base, get_session_factory
from lab_tracker.models import QuestionType, SessionType
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


def _actor() -> AuthContext:
    return AuthContext(user_id=uuid4(), role=Role.ADMIN)


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_repository_backed_api_persists_core_entities(tmp_path):
    db_path = tmp_path / "api-persistence.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = get_session_factory(engine=engine)
    actor = _actor()

    with session_factory() as session:
        api = LabTrackerAPI(repository=SQLAlchemyLabTrackerRepository(session))
        project = api.create_project("Persistence Project", actor=actor)
        question = api.create_question(
            project_id=project.project_id,
            text="Is persistence wired?",
            question_type=QuestionType.DESCRIPTIVE,
            actor=actor,
        )
        dataset = api.create_dataset(
            project_id=project.project_id,
            primary_question_id=question.question_id,
            actor=actor,
        )
        note = api.create_note(
            project_id=project.project_id,
            raw_content="meeting notes",
            actor=actor,
        )
        created_session = api.create_session(
            project_id=project.project_id,
            session_type=SessionType.OPERATIONAL,
            actor=actor,
        )
        api.update_project(
            project.project_id,
            description="updated description",
            actor=actor,
        )

    with session_factory() as session:
        api = LabTrackerAPI(repository=SQLAlchemyLabTrackerRepository(session))
        loaded_project = api.get_project(project.project_id)
        assert loaded_project.description == "updated description"
        assert api.get_question(question.question_id).project_id == project.project_id
        assert api.get_dataset(dataset.dataset_id).primary_question_id == question.question_id
        assert api.get_note(note.note_id).raw_content == "meeting notes"
        assert api.get_session(created_session.session_id).project_id == project.project_id

    engine.dispose()


def test_fastapi_routes_persist_across_app_restarts(monkeypatch, tmp_path):
    db_path = tmp_path / "route-persistence.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", str(tmp_path / "note-storage"))

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    engine.dispose()

    app_first = create_app()
    with TestClient(app_first) as client:
        register_response = client.post(
            "/auth/register",
            json={
                "username": "route-persistence-admin",
                "password": "secret",
                "role": "admin",
            },
        )
        assert register_response.status_code == 201
        token = register_response.json()["data"]["access_token"]
        headers = _auth_headers(token)

        create_response = client.post(
            "/projects",
            json={"name": "Route Persistence"},
            headers=headers,
        )
        assert create_response.status_code == 201
        project_id = create_response.json()["data"]["project_id"]
        question_response = client.post(
            "/questions",
            json={
                "project_id": project_id,
                "text": "Can routes persist question data?",
                "question_type": "descriptive",
            },
            headers=headers,
        )
        assert question_response.status_code == 201
        question_id = question_response.json()["data"]["question_id"]
        dataset_response = client.post(
            "/datasets",
            json={
                "project_id": project_id,
                "primary_question_id": question_id,
            },
            headers=headers,
        )
        assert dataset_response.status_code == 201
        dataset_id = dataset_response.json()["data"]["dataset_id"]

    app_second = create_app()
    with TestClient(app_second) as client:
        login_response = client.post(
            "/auth/login",
            json={
                "username": "route-persistence-admin",
                "password": "secret",
            },
        )
        assert login_response.status_code == 200
        token = login_response.json()["data"]["access_token"]
        headers = _auth_headers(token)

        list_response = client.get("/projects", headers=headers)
        question_list_response = client.get("/questions", headers=headers)
        dataset_list_response = client.get("/datasets", headers=headers)
    assert list_response.status_code == 200
    assert question_list_response.status_code == 200
    assert dataset_list_response.status_code == 200
    project_ids = [item["project_id"] for item in list_response.json()["data"]]
    question_ids = [item["question_id"] for item in question_list_response.json()["data"]]
    dataset_ids = [item["dataset_id"] for item in dataset_list_response.json()["data"]]
    assert project_id in project_ids
    assert question_id in question_ids
    assert dataset_id in dataset_ids
