from __future__ import annotations

from uuid import uuid4

from api_helpers import register_test_resources, repository_backed_api
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.db import Base
from lab_tracker.services import (
    AnalysisService,
    ClaimService,
    DatasetService,
    GraphDraftService,
    NoteService,
    ProjectAuthorizationPolicy,
    ProjectService,
    QuestionService,
    ServiceContext,
    SessionService,
    VisualizationService,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

SERVICE_TYPES = (
    ProjectService,
    QuestionService,
    DatasetService,
    SessionService,
    NoteService,
    AnalysisService,
    ClaimService,
    VisualizationService,
    GraphDraftService,
)


def _actor() -> AuthContext:
    return AuthContext(user_id=uuid4(), role=Role.ADMIN)


def _repository() -> SQLAlchemyLabTrackerRepository:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    session = session_factory()
    repository = SQLAlchemyLabTrackerRepository(session)
    repository._test_resources = (engine, session)  # type: ignore[attr-defined]
    register_test_resources(engine, session)
    return repository


def test_api_no_longer_subclasses_service_classes() -> None:
    assert all(not issubclass(LabTrackerAPI, service_type) for service_type in SERVICE_TYPES)


def test_api_composes_named_service_instances() -> None:
    api = repository_backed_api()

    assert isinstance(api.project_authorization, ProjectAuthorizationPolicy)
    assert isinstance(api.projects, ProjectService)
    assert isinstance(api.questions, QuestionService)
    assert isinstance(api.datasets, DatasetService)
    assert isinstance(api.sessions, SessionService)
    assert isinstance(api.notes, NoteService)
    assert isinstance(api.analyses, AnalysisService)
    assert isinstance(api.claims, ClaimService)
    assert isinstance(api.visualizations, VisualizationService)
    assert isinstance(api.graph_drafts, GraphDraftService)
    assert api.questions.projects is api.projects
    assert api.datasets.sessions is api.sessions
    assert api.graph_drafts.notes is api.notes
    assert api.projects.authorization is api.project_authorization
    assert api.questions.authorization is api.project_authorization
    assert api.datasets.authorization is api.project_authorization
    assert api.sessions.authorization is api.project_authorization
    assert api.notes.authorization is api.project_authorization
    assert api.analyses.authorization is api.project_authorization
    assert api.claims.authorization is api.project_authorization
    assert api.visualizations.authorization is api.project_authorization
    assert api.graph_drafts.authorization is api.project_authorization


def test_project_service_can_run_without_api_facade() -> None:
    context = ServiceContext(repository=_repository())
    authorization = ProjectAuthorizationPolicy(context)
    service = ProjectService(context, authorization=authorization)
    project = service.create_project("Standalone service", actor=_actor())

    assert service.get_project(project.project_id).name == "Standalone service"


def test_flat_facade_delegates_to_composed_services() -> None:
    api = repository_backed_api()
    actor = _actor()

    project = api.create_project("Facade project", actor=actor)
    updated = api.update_project(project.project_id, name="Delegated project", actor=actor)

    assert updated.name == "Delegated project"
    assert api.projects.get_project(project.project_id).name == "Delegated project"
