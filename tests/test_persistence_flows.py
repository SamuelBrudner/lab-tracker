from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from lab_tracker.api import LabTrackerAPI
from lab_tracker.app import create_app
from lab_tracker.auth import AuthContext, Role
from lab_tracker.db import Base, get_session_factory
from lab_tracker.db_models import ProjectModel
from lab_tracker.models import QuestionType, SessionType
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


def _actor() -> AuthContext:
    return AuthContext(user_id=uuid4(), role=Role.ADMIN)


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_admin(app, *, username: str, password: str) -> None:
    app.state.auth_service.register_user(
        username=username,
        password=password,
        role=Role.ADMIN,
    )


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
        created_output = api.register_acquisition_output(
            created_session.session_id,
            file_path="capture/output.bin",
            checksum="abc123",
            size_bytes=12,
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
        outputs = api.list_acquisition_outputs(session_id=created_session.session_id)
        assert outputs == [created_output]

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
    _seed_admin(
        app_first,
        username="route-persistence-admin",
        password="secret",
    )
    with TestClient(app_first) as client:
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
        session_response = client.post(
            "/sessions",
            json={
                "project_id": project_id,
                "session_type": "operational",
            },
            headers=headers,
        )
        assert session_response.status_code == 201
        session_id = session_response.json()["data"]["session_id"]
        output_response = client.post(
            f"/sessions/{session_id}/outputs",
            json={
                "file_path": "capture/output.bin",
                "checksum": "abc123",
                "size_bytes": 64,
            },
            headers=headers,
        )
        assert output_response.status_code == 201
        output_id = output_response.json()["data"]["output_id"]

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
        session_output_response = client.get(f"/sessions/{session_id}/outputs", headers=headers)
    assert list_response.status_code == 200
    assert question_list_response.status_code == 200
    assert dataset_list_response.status_code == 200
    assert session_output_response.status_code == 200
    project_ids = [item["project_id"] for item in list_response.json()["data"]]
    question_ids = [item["question_id"] for item in question_list_response.json()["data"]]
    dataset_ids = [item["dataset_id"] for item in dataset_list_response.json()["data"]]
    output_ids = [item["output_id"] for item in session_output_response.json()["data"]]
    assert project_id in project_ids
    assert question_id in question_ids
    assert dataset_id in dataset_ids
    assert output_id in output_ids


def test_fastapi_routes_read_database_changes_after_app_start(monkeypatch, tmp_path):
    db_path = tmp_path / "route-refresh.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    engine.dispose()

    app = create_app()
    _seed_admin(
        app,
        username="route-refresh-admin",
        password="secret",
    )

    with app.state.db_session_factory() as session:
        session.add(ProjectModel(name="Inserted after startup", description="external"))
        session.commit()

    with TestClient(app) as client:
        login_response = client.post(
            "/auth/login",
            json={
                "username": "route-refresh-admin",
                "password": "secret",
            },
        )
        assert login_response.status_code == 200
        headers = _auth_headers(login_response.json()["data"]["access_token"])
        list_response = client.get("/projects", headers=headers)

    assert list_response.status_code == 200
    names = [item["name"] for item in list_response.json()["data"]]
    assert "Inserted after startup" in names


def test_note_metadata_search_survives_app_restart(monkeypatch, tmp_path):
    db_path = tmp_path / "note-search-persistence.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    monkeypatch.setenv("LAB_TRACKER_FILE_STORAGE_PATH", str(tmp_path / "file-storage"))
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", str(tmp_path / "note-storage"))

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    engine.dispose()

    app_first = create_app()
    _seed_admin(
        app_first,
        username="search-persistence-admin",
        password="secret",
    )
    with TestClient(app_first) as client:
        login_response = client.post(
            "/auth/login",
            json={
                "username": "search-persistence-admin",
                "password": "secret",
            },
        )
        assert login_response.status_code == 200
        headers = _auth_headers(login_response.json()["data"]["access_token"])

        project_id = client.post(
            "/projects",
            json={"name": "Metadata search"},
            headers=headers,
        ).json()["data"]["project_id"]
        note_response = client.post(
            "/notes",
            json={
                "project_id": project_id,
                "raw_content": "capture log",
                "metadata": {"owner": "Sam", "rig": "np2"},
            },
            headers=headers,
        )
        assert note_response.status_code == 201
        note_id = note_response.json()["data"]["note_id"]

        search_response = client.get(
            "/search",
            params={"q": "sam", "project_id": project_id},
            headers=headers,
        )
        assert search_response.status_code == 200
        search_ids = [item["note_id"] for item in search_response.json()["data"]["notes"]]
        assert note_id in search_ids

    app_second = create_app()
    with TestClient(app_second) as client:
        login_response = client.post(
            "/auth/login",
            json={
                "username": "search-persistence-admin",
                "password": "secret",
            },
        )
        assert login_response.status_code == 200
        headers = _auth_headers(login_response.json()["data"]["access_token"])

        search_response = client.get(
            "/search",
            params={"q": "sam", "project_id": project_id},
            headers=headers,
        )

    assert search_response.status_code == 200
    search_ids = [item["note_id"] for item in search_response.json()["data"]["notes"]]
    assert note_id in search_ids


def test_repository_backed_api_rolls_back_failed_writes_from_read_state():
    class _EmptyRepoPart:
        def list(self):
            return []

        def get(self, entity_id):  # noqa: ANN001
            return None

        def save(self, entity):  # noqa: ANN001
            return None

        def delete(self, entity_id):  # noqa: ANN001
            return None

    class _FailingProjects(_EmptyRepoPart):
        def save(self, entity):  # noqa: ANN001
            raise RuntimeError("db write failed")

    class _Repo:
        def __init__(self) -> None:
            self.projects = _FailingProjects()
            other = _EmptyRepoPart()
            self.questions = other
            self.datasets = other
            self.dataset_reviews = other
            self.notes = other
            self.sessions = other
            self.acquisition_outputs = other
            self.analyses = other
            self.claims = other
            self.visualizations = other

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    api = LabTrackerAPI(repository=_Repo())
    actor = _actor()

    try:
        api.create_project("Will fail", actor=actor)
    except RuntimeError as exc:
        assert str(exc) == "db write failed"
    else:  # pragma: no cover
        raise AssertionError("Expected repository write to fail")

    assert api.list_projects() == []
