from __future__ import annotations

from fastapi import Request
from fastapi.testclient import TestClient

from lab_tracker.app import create_app
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


class _SessionSpy:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closes += 1


class _SessionFactorySpy:
    def __init__(self) -> None:
        self.sessions: list[_SessionSpy] = []

    def __call__(self) -> _SessionSpy:
        session = _SessionSpy()
        self.sessions.append(session)
        return session


def test_db_session_middleware_commits_and_closes_on_success():
    app = create_app()
    factory = _SessionFactorySpy()
    app.state.db_session_factory = factory

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert len(factory.sessions) == 1
    session = factory.sessions[0]
    assert session.commits == 1
    assert session.rollbacks == 0
    assert session.closes == 1


def test_db_session_middleware_rolls_back_and_closes_on_error():
    app = create_app()
    factory = _SessionFactorySpy()
    app.state.db_session_factory = factory

    @app.get("/_test/fail")
    def fail_route():
        raise ValueError("intentional failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/_test/fail")

    assert response.status_code == 500
    assert len(factory.sessions) == 1
    session = factory.sessions[0]
    assert session.commits == 0
    assert session.rollbacks == 1
    assert session.closes == 1


def test_global_repository_dependency_is_wired():
    app = create_app()

    @app.get("/_test/repository")
    def repository_probe(request: Request):
        repository = getattr(request.state, "lab_tracker_repository", None)
        db_session = getattr(request.state, "db_session", None)
        return {
            "has_repository": isinstance(repository, SQLAlchemyLabTrackerRepository),
            "shares_session": repository is not None and repository._session is db_session,
        }

    with TestClient(app) as client:
        response = client.get("/_test/repository")

    payload = response.json()
    assert response.status_code == 200
    assert payload["has_repository"] is True
    assert payload["shares_session"] is True
