from __future__ import annotations

from uuid import uuid4

from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from lab_tracker.app import create_app
from lab_tracker.app_parts.runtime import _log_startup_config_summary
from lab_tracker.application import RequestHandlers
from lab_tracker.config import Settings
from lab_tracker.db import Base, get_engine
from lab_tracker.db_models import ProjectModel, QuestionModel
from lab_tracker.errors import ValidationError
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


class _LoggerSpy:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, tuple[object, ...], dict[str, object]]] = []

    def info(self, message: str, *args: object, **kwargs: object) -> None:
        self.records.append(("info", message, args, kwargs))

    def warning(self, message: str, *args: object, **kwargs: object) -> None:
        self.records.append(("warning", message, args, kwargs))

    def error(self, message: str, *args: object, **kwargs: object) -> None:
        self.records.append(("error", message, args, kwargs))


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


def test_unhandled_exceptions_return_error_envelope_and_log(monkeypatch):
    app = create_app()
    logger = _LoggerSpy()
    monkeypatch.setattr("lab_tracker.routes.errors._logger", logger)

    @app.get("/_test/unhandled")
    def unhandled_route():
        raise RuntimeError("intentional failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/_test/unhandled")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_server_error",
            "message": "Internal server error.",
            "issues": None,
        }
    }
    assert logger.records
    level, message, args, kwargs = logger.records[0]
    assert level == "error"
    assert "Unhandled HTTP exception" in message
    assert args == ("GET", "/_test/unhandled", 500)
    assert "exc_info" in kwargs


def test_handled_lab_tracker_errors_are_logged(monkeypatch):
    app = create_app()
    logger = _LoggerSpy()
    monkeypatch.setattr("lab_tracker.routes.errors._logger", logger)

    @app.get("/_test/handled")
    def handled_route():
        raise ValidationError("bad request")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/_test/handled")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert logger.records
    level, message, args, kwargs = logger.records[0]
    assert level == "warning"
    assert "Handled HTTP error" in message
    assert args[:4] == ("GET", "/_test/handled", 422, "validation_error")
    assert str(args[4]) == "bad request"
    assert kwargs == {}


def test_request_handlers_share_the_middleware_transaction_identity():
    app = create_app()

    @app.get("/_test/handlers")
    def handler_probe(request: Request):
        handlers = getattr(request.state, "lab_tracker_handlers", None)
        request_api = getattr(request.state, "lab_tracker_api", None)
        repositories = [
            handlers.catalogs.repository,
            handlers.context.repository,
            handlers.dataset_files.repository,
        ]
        sessions = [
            handlers.context.session,
            handlers.dataset_files.session,
            handlers.visualization_files.session,
            handlers.deletions.session,
        ]
        return {
            "has_handlers": isinstance(handlers, RequestHandlers),
            "shares_api": all(
                handler.api is request_api
                for handler in (
                    handlers.catalogs,
                    handlers.context,
                    handlers.dataset_files,
                    handlers.visualization_files,
                    handlers.deletions,
                )
            ),
            "shares_repository": (
                isinstance(repositories[0], SQLAlchemyLabTrackerRepository)
                and all(repository is repositories[0] for repository in repositories)
            ),
            "shares_session": all(
                session is repositories[0]._session for session in sessions
            ),
            "raw_dependencies_hidden": (
                not hasattr(request.state, "lab_tracker_repository")
                and not hasattr(request.state, "db_session")
            ),
        }

    with TestClient(app) as client:
        response = client.get("/_test/handlers")

    payload = response.json()
    assert response.status_code == 200
    assert payload["has_handlers"] is True
    assert payload["shares_api"] is True
    assert payload["shares_repository"] is True
    assert payload["shares_session"] is True
    assert payload["raw_dependencies_hidden"] is True


def test_sqlite_engine_enforces_foreign_keys_and_busy_wal_pragmas(tmp_path):
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'sqlite-pragmas.db'}",
        environment="local",
        auth_enabled=False,
        _env_file=None,
    )
    engine = get_engine(settings)
    try:
        Base.metadata.create_all(bind=engine)

        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"

        project_id = str(uuid4())
        question_id = str(uuid4())
        with Session(engine) as session:
            session.add(ProjectModel(project_id=project_id, name="Cascade project"))
            session.add(
                QuestionModel(
                    question_id=question_id,
                    project_id=project_id,
                    text="Does SQLite enforce cascades?",
                    question_type="descriptive",
                )
            )
            session.commit()

            session.execute(delete(ProjectModel).where(ProjectModel.project_id == project_id))
            session.commit()

            assert (
                session.scalar(
                    select(QuestionModel).where(QuestionModel.question_id == question_id)
                )
                is None
            )
    finally:
        engine.dispose()


def test_startup_config_summary_logs_environment_db_backend_and_auth(
    tmp_path,
    monkeypatch,
):
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'startup-summary.db'}",
        environment="local",
        auth_enabled=False,
        _env_file=None,
    )
    engine = get_engine(settings)
    logger = _LoggerSpy()
    monkeypatch.setattr("lab_tracker.app_parts.runtime._logger", logger)
    try:
        _log_startup_config_summary(settings, engine=engine, auth_enabled=False)
    finally:
        engine.dispose()

    assert logger.records == [
        (
            "info",
            "Lab Tracker startup: environment=%s database_backend=%s auth_enabled=%s",
            ("local", "sqlite", False),
            {},
        )
    ]


def test_raw_sqlite_test_engines_enforce_foreign_keys_and_busy_wal_pragmas(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'raw-sqlite-pragmas.db'}",
        future=True,
    )
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
    finally:
        engine.dispose()


def test_db_session_middleware_runs_after_commit_actions_once():
    app = create_app()
    events: list[str] = []

    @app.get("/_test/after-commit")
    def after_commit_probe(request: Request):
        request_api = request.state.lab_tracker_api
        request_api.run_after_commit(lambda: events.append("commit"))
        request_api.run_after_rollback(lambda: events.append("rollback"))
        return {"status": "ok"}

    with TestClient(app) as client:
        response = client.get("/_test/after-commit")

    assert response.status_code == 200
    assert events == ["commit"]


def test_db_session_middleware_runs_after_rollback_actions_on_error_response():
    app = create_app()
    events: list[str] = []

    @app.get("/_test/after-rollback")
    def after_rollback_probe(request: Request):
        request_api = request.state.lab_tracker_api
        request_api.run_after_commit(lambda: events.append("commit"))
        request_api.run_after_rollback(lambda: events.append("rollback"))
        return JSONResponse(status_code=409, content={"error": "conflict"})

    with TestClient(app) as client:
        response = client.get("/_test/after-rollback")

    assert response.status_code == 409
    assert events == ["rollback"]


def test_db_session_middleware_runs_after_rollback_actions_on_exception():
    app = create_app()
    events: list[str] = []

    @app.get("/_test/after-rollback-exception")
    def after_rollback_exception_probe(request: Request):
        request_api = request.state.lab_tracker_api
        request_api.run_after_commit(lambda: events.append("commit"))
        request_api.run_after_rollback(lambda: events.append("rollback"))
        raise ValueError("intentional failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/_test/after-rollback-exception")

    assert response.status_code == 500
    assert events == ["rollback"]
