from __future__ import annotations

from uuid import uuid4

import pytest
from api_helpers import repository_backed_api
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from lab_tracker.api import LabTrackerAPI
from lab_tracker.app import create_app
from lab_tracker.auth import LOCAL_AUTH_USER_ID, AuthContext, Role
from lab_tracker.db import Base, get_session_factory
from lab_tracker.db_models import NoteModel, ProjectModel, QuestionModel, UserModel
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    AnalysisStatus,
    ClaimStatus,
    DatasetCommitManifestInput,
    DatasetStatus,
    EntityRef,
    EntityType,
    GoalRelation,
    GoalType,
    ProjectMembershipRole,
    QuestionStatus,
    QuestionType,
    SessionType,
)
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


class _AttributionDraftClient:
    provider = "fake"
    model = "fake-attribution-model"

    def draft_from_note(self, **_kwargs):
        return {
            "summary": "empty",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [],
        }

    def draft_from_batch(self, **kwargs):
        batch_context = kwargs.get("batch_context") or {}
        return {
            **self.draft_from_note(),
            "note_dispositions": [
                {
                    "note_id": str(note_id),
                    "disposition": "no_change",
                    "reason": "considered by fake drafter",
                    "evidence_quote": "",
                    "client_refs": [],
                }
                for note_id in batch_context.get("note_ids_requiring_disposition") or []
            ],
        }


_ATTRIBUTION_COLUMN_PAIRS = (
    ("project_groups", "created_by", "created_by_user_id"),
    ("projects", "created_by", "created_by_user_id"),
    ("group_memberships", "created_by", "created_by_user_id"),
    ("project_memberships", "created_by", "created_by_user_id"),
    ("questions", "created_by", "created_by_user_id"),
    ("question_refactors", "created_by", "created_by_user_id"),
    ("datasets", "created_by", "created_by_user_id"),
    ("notes", "created_by", "created_by_user_id"),
    ("sessions", "created_by", "created_by_user_id"),
    ("analyses", "executed_by", "executed_by_user_id"),
    ("goals", "created_by", "created_by_user_id"),
    ("goal_links", "created_by", "created_by_user_id"),
    ("graph_change_sets", "created_by", "created_by_user_id"),
    ("graph_draft_batch_runs", "created_by", "created_by_user_id"),
)


def _insert_user(session, user_id, username, role=Role.ADMIN):
    session.add(
        UserModel(
            user_id=str(user_id),
            username=username,
            password_hash="unused",
            role=role.value,
        )
    )


def _assert_attribution_row(
    session,
    *,
    table_name,
    id_column,
    id_value,
    legacy_column,
    fk_column,
    expected_legacy,
    expected_fk,
):
    row = session.execute(
        text(
            f"SELECT {legacy_column}, {fk_column} "
            f"FROM {table_name} "
            f"WHERE {id_column} = :id_value"
        ),
        {"id_value": str(id_value)},
    ).mappings().one()
    assert row[legacy_column] == expected_legacy
    assert row[fk_column] == expected_fk


def _assert_no_attribution_divergence(session):
    for table_name, legacy_column, fk_column in _ATTRIBUTION_COLUMN_PAIRS:
        divergent_count = session.execute(
            text(
                f"SELECT COUNT(*) "
                f"FROM {table_name} "
                f"WHERE {legacy_column} IS NOT NULL "
                f"AND {fk_column} IS NOT NULL "
                f"AND {legacy_column} != {fk_column}"
            )
        ).scalar_one()
        assert divergent_count == 0, table_name


class _SpySearchRepository(SQLAlchemyLabTrackerRepository):
    def __init__(self, session) -> None:  # noqa: ANN001
        super().__init__(session)
        self.last_note_query: dict[str, object] | None = None
        self.last_question_query: dict[str, object] | None = None

    def query_questions(self, **kwargs):  # noqa: ANN003
        self.last_question_query = dict(kwargs)
        return super().query_questions(**kwargs)

    def query_notes(self, **kwargs):  # noqa: ANN003
        self.last_note_query = dict(kwargs)
        return super().query_notes(**kwargs)


class _SpyQueryRepository(SQLAlchemyLabTrackerRepository):
    def __init__(self, session) -> None:  # noqa: ANN001
        super().__init__(session)
        self.calls: dict[str, dict[str, object]] = {}

    def _remember(self, name: str, kwargs: dict[str, object]) -> None:
        self.calls[name] = dict(kwargs)

    def query_datasets(self, **kwargs):  # noqa: ANN003
        self._remember("datasets", kwargs)
        return super().query_datasets(**kwargs)

    def query_notes(self, **kwargs):  # noqa: ANN003
        self._remember("notes", kwargs)
        return super().query_notes(**kwargs)

    def query_sessions(self, **kwargs):  # noqa: ANN003
        self._remember("sessions", kwargs)
        return super().query_sessions(**kwargs)

    def query_acquisition_outputs(self, **kwargs):  # noqa: ANN003
        self._remember("acquisition_outputs", kwargs)
        return super().query_acquisition_outputs(**kwargs)

    def query_analyses(self, **kwargs):  # noqa: ANN003
        self._remember("analyses", kwargs)
        return super().query_analyses(**kwargs)

    def query_claims(self, **kwargs):  # noqa: ANN003
        self._remember("claims", kwargs)
        return super().query_claims(**kwargs)

    def query_visualizations(self, **kwargs):  # noqa: ANN003
        self._remember("visualizations", kwargs)
        return super().query_visualizations(**kwargs)


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


def test_repository_backed_api_dual_writes_attribution_user_fk_columns(tmp_path):
    db_path = tmp_path / "api-attribution-fks.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = get_session_factory(engine=engine)
    actor_id = uuid4()
    target_user_id = uuid4()
    actor = AuthContext(user_id=actor_id, role=Role.ADMIN)

    with session_factory() as session:
        _insert_user(session, actor_id, "attribution-admin", Role.ADMIN)
        _insert_user(session, target_user_id, "attribution-viewer", Role.VIEWER)
        session.commit()

    with session_factory() as session:
        api = LabTrackerAPI(repository=SQLAlchemyLabTrackerRepository(session))
        group = api.create_project_group("Attribution group", actor=actor)
        project = api.create_project(
            "Attribution project",
            group_id=group.group_id,
            actor=actor,
        )
        group_membership = api.upsert_group_membership(
            group.group_id,
            target_user_id,
            ProjectMembershipRole.VIEWER,
            actor=actor,
        )
        project_membership = api.upsert_project_membership(
            project.project_id,
            target_user_id,
            ProjectMembershipRole.CONTRIBUTOR,
            actor=actor,
        )
        question = api.create_question(
            project_id=project.project_id,
            text="Which attribution fields stay linked?",
            question_type=QuestionType.DESCRIPTIVE,
            actor=actor,
        )
        refactor_source = api.create_question(
            project_id=project.project_id,
            text="Can this question be refined?",
            question_type=QuestionType.DESCRIPTIVE,
            actor=actor,
        )
        refactor = api.refactor_question(
            refactor_source.question_id,
            replacement_text="Can this refined question keep attribution?",
            replacement_question_type=QuestionType.DESCRIPTIVE,
            replacement_status=QuestionStatus.STAGED,
            reason="Exercise attribution mirror writes.",
            actor=actor,
        ).refactor
        dataset = api.create_dataset(
            project_id=project.project_id,
            primary_question_id=question.question_id,
            actor=actor,
        )
        note = api.create_note(
            project_id=project.project_id,
            raw_content="attribution note",
            actor=actor,
        )
        created_session = api.create_session(
            project_id=project.project_id,
            session_type=SessionType.OPERATIONAL,
            actor=actor,
        )
        analysis = api.create_analysis(
            project_id=project.project_id,
            dataset_ids=[dataset.dataset_id],
            method_hash="method",
            code_version="v1",
            actor=actor,
        )
        goal = api.create_goal(
            project_id=project.project_id,
            goal_type=GoalType.PAPER,
            title="Attribution manuscript",
            actor=actor,
        )
        goal_link = api.link_node_to_goal(
            goal.goal_id,
            target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id),
            relation=GoalRelation.ADDRESSES,
            actor=actor,
        )
        draft_client = _AttributionDraftClient()
        change_set = api.create_graph_draft_from_note(
            note.note_id,
            draft_client=draft_client,
            actor=actor,
        )
        batch_run = api.run_graph_draft_batch_for_project(
            project.project_id,
            draft_client=draft_client,
            actor=actor,
        )

        assert project.created_by_user_id == actor_id
        assert analysis.executed_by_user_id == actor_id
        assert batch_run.created_by_user_id == actor_id

        expected_user = str(actor_id)
        created_rows = [
            ("project_groups", "group_id", group.group_id),
            ("projects", "project_id", project.project_id),
            ("group_memberships", "membership_id", group_membership.membership_id),
            ("project_memberships", "membership_id", project_membership.membership_id),
            ("questions", "question_id", question.question_id),
            ("question_refactors", "refactor_id", refactor.refactor_id),
            ("datasets", "dataset_id", dataset.dataset_id),
            ("notes", "note_id", note.note_id),
            ("sessions", "session_id", created_session.session_id),
            ("goals", "goal_id", goal.goal_id),
            ("goal_links", "link_id", goal_link.link_id),
            ("graph_change_sets", "change_set_id", change_set.change_set_id),
            ("graph_draft_batch_runs", "run_id", batch_run.run_id),
        ]
        if batch_run.change_set_id is not None:
            created_rows.append(
                ("graph_change_sets", "change_set_id", batch_run.change_set_id)
            )
        for table_name, id_column, id_value in created_rows:
            _assert_attribution_row(
                session,
                table_name=table_name,
                id_column=id_column,
                id_value=id_value,
                legacy_column="created_by",
                fk_column="created_by_user_id",
                expected_legacy=expected_user,
                expected_fk=expected_user,
            )
        _assert_attribution_row(
            session,
            table_name="analyses",
            id_column="analysis_id",
            id_value=analysis.analysis_id,
            legacy_column="executed_by",
            fk_column="executed_by_user_id",
            expected_legacy=expected_user,
            expected_fk=expected_user,
        )
        _assert_no_attribution_divergence(session)

    engine.dispose()


def test_repository_backed_api_leaves_local_attribution_fk_null(tmp_path):
    db_path = tmp_path / "api-local-attribution.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = get_session_factory(engine=engine)
    actor = AuthContext(user_id=LOCAL_AUTH_USER_ID, role=Role.ADMIN)

    with session_factory() as session:
        api = LabTrackerAPI(repository=SQLAlchemyLabTrackerRepository(session))
        project = api.create_project("Local attribution project", actor=actor)

        assert project.created_by == str(LOCAL_AUTH_USER_ID)
        assert project.created_by_user_id is None
        _assert_attribution_row(
            session,
            table_name="projects",
            id_column="project_id",
            id_value=project.project_id,
            legacy_column="created_by",
            fk_column="created_by_user_id",
            expected_legacy=str(LOCAL_AUTH_USER_ID),
            expected_fk=None,
        )

    engine.dispose()


def test_repository_backed_api_updates_existing_acquisition_output_without_store_cache(tmp_path):
    db_path = tmp_path / "api-output-upsert.db"
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
        created_session = api.create_session(
            project_id=project.project_id,
            session_type=SessionType.OPERATIONAL,
            actor=actor,
        )
        first = api.register_acquisition_output(
            created_session.session_id,
            file_path="capture/output.bin",
            checksum="abc123",
            size_bytes=12,
            actor=actor,
        )
        updated = api.register_acquisition_output(
            created_session.session_id,
            file_path="capture/output.bin",
            checksum="def456",
            size_bytes=24,
            actor=actor,
        )

        assert updated.output_id == first.output_id
        assert updated.checksum == "def456"
        assert updated.size_bytes == 24
        assert len(api.list_acquisition_outputs(session_id=created_session.session_id)) == 1

    engine.dispose()


def test_repository_backed_api_rejects_question_parent_cycles_without_store_cache(tmp_path):
    db_path = tmp_path / "api-question-cycles.db"
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
        parent = api.create_question(
            project_id=project.project_id,
            text="Parent question",
            question_type=QuestionType.DESCRIPTIVE,
            actor=actor,
        )
        child = api.create_question(
            project_id=project.project_id,
            text="Child question",
            question_type=QuestionType.DESCRIPTIVE,
            parent_question_ids=[parent.question_id],
            actor=actor,
        )

        with pytest.raises(ValidationError, match="acyclic"):
            api.update_question(
                parent.question_id,
                parent_question_ids=[child.question_id],
                actor=actor,
            )

    engine.dispose()


def test_repository_backed_api_search_helpers_delegate_to_repository_queries(tmp_path):
    db_path = tmp_path / "api-search.db"
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
        project = api.create_project("Search Project", actor=actor)
        api.create_question(
            project_id=project.project_id,
            text="Baseline question",
            question_type=QuestionType.DESCRIPTIVE,
            actor=actor,
        )
        api.create_question(
            project_id=project.project_id,
            text="Control question",
            question_type=QuestionType.DESCRIPTIVE,
            actor=actor,
        )
        api.create_note(
            project_id=project.project_id,
            raw_content="baseline capture",
            actor=actor,
        )
        api.create_note(
            project_id=project.project_id,
            raw_content="raw capture",
            transcribed_text="baseline transcript",
            actor=actor,
        )

    with session_factory() as session:
        repository = _SpySearchRepository(session)
        api = LabTrackerAPI(repository=repository)

        filtered_questions = api.list_questions_filtered(
            project_id=project.project_id,
            search="baseline",
        )

        assert repository.last_question_query == {
            "ancestor_question_id": None,
            "limit": None,
            "offset": 0,
            "parent_question_id": None,
            "project_id": project.project_id,
            "question_type": None,
            "search": "baseline",
            "status": None,
        }
        assert [question.text for question in filtered_questions] == ["Baseline question"]

        repository.last_question_query = None
        searched_questions = api.search_questions(
            "baseline",
            project_id=project.project_id,
            limit=1,
            offset=0,
        )

        assert repository.last_question_query == {
            "limit": 1,
            "offset": 0,
            "project_id": project.project_id,
            "search": "baseline",
        }
        assert len(searched_questions) == 1
        assert searched_questions[0].text == "Baseline question"

        searched_notes = api.search_notes(
            "baseline",
            project_id=project.project_id,
            limit=1,
            offset=0,
        )

        assert repository.last_note_query == {
            "limit": 1,
            "offset": 0,
            "project_id": project.project_id,
            "search": "baseline",
        }
        assert len(searched_notes) == 1

    engine.dispose()


def test_repository_backed_api_list_helpers_delegate_to_repository_queries(tmp_path):
    db_path = tmp_path / "api-list-queries.db"
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
        project = api.create_project("List Query Project", actor=actor)
        question = api.create_question(
            project_id=project.project_id,
            text="Primary question",
            question_type=QuestionType.DESCRIPTIVE,
            status=QuestionStatus.ACTIVE,
            actor=actor,
        )
        dataset = api.create_dataset(
            project_id=project.project_id,
            primary_question_id=question.question_id,
            status=DatasetStatus.COMMITTED,
            commit_manifest=DatasetCommitManifestInput(
                files=[
                    {
                        "checksum": "checksum-1",
                        "path": "capture/data.bin",
                        "size_bytes": 10,
                    }
                ]
            ),
            actor=actor,
        )
        created_note = api.create_note(
            project_id=project.project_id,
            raw_content="session note",
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
        created_analysis = api.create_analysis(
            project_id=project.project_id,
            dataset_ids=[dataset.dataset_id],
            method_hash="method-1",
            code_version="code-1",
            status=AnalysisStatus.COMMITTED,
            actor=actor,
        )
        created_claim = api.create_claim(
            project_id=project.project_id,
            statement="Supported claim",
            confidence=0.9,
            status=ClaimStatus.PROPOSED,
            supported_by_dataset_ids=[dataset.dataset_id],
            supported_by_analysis_ids=[created_analysis.analysis_id],
            actor=actor,
        )
        created_visualization = api.create_visualization(
            analysis_id=created_analysis.analysis_id,
            viz_type="heatmap",
            file_path="viz/heatmap.png",
            related_claim_ids=[created_claim.claim_id],
            actor=actor,
        )

    with session_factory() as session:
        repository = _SpyQueryRepository(session)
        api = LabTrackerAPI(repository=repository)

        assert api.list_notes(project_id=project.project_id) == [created_note]
        assert repository.calls["notes"] == {
            "limit": None,
            "offset": 0,
            "project_id": project.project_id,
            "status": None,
            "target_entity_id": None,
            "target_entity_type": None,
        }

        assert api.list_sessions(project_id=project.project_id) == [created_session]
        assert repository.calls["sessions"] == {
            "limit": None,
            "offset": 0,
            "project_id": project.project_id,
        }

        assert api.list_acquisition_outputs(session_id=created_session.session_id) == [
            created_output
        ]
        assert repository.calls["acquisition_outputs"] == {
            "limit": None,
            "offset": 0,
            "session_id": created_session.session_id,
        }

        assert api.list_datasets(project_id=project.project_id) == [dataset]
        assert repository.calls["datasets"] == {
            "limit": None,
            "offset": 0,
            "project_id": project.project_id,
        }

        assert api.list_analyses(project_id=project.project_id, dataset_id=dataset.dataset_id) == [
            created_analysis
        ]
        assert repository.calls["analyses"] == {
            "dataset_id": dataset.dataset_id,
            "limit": None,
            "offset": 0,
            "project_id": project.project_id,
            "question_id": None,
        }

        assert api.list_claims(
            project_id=project.project_id,
            dataset_id=dataset.dataset_id,
            analysis_id=created_analysis.analysis_id,
        ) == [created_claim]
        assert repository.calls["claims"] == {
            "analysis_id": created_analysis.analysis_id,
            "dataset_id": dataset.dataset_id,
            "limit": None,
            "offset": 0,
            "project_id": project.project_id,
            "status": None,
        }

        assert api.list_visualizations(
            project_id=project.project_id,
            analysis_id=created_analysis.analysis_id,
            claim_id=created_claim.claim_id,
        ) == [created_visualization]
        assert repository.calls["visualizations"] == {
            "analysis_id": created_analysis.analysis_id,
            "claim_id": created_claim.claim_id,
            "limit": None,
            "offset": 0,
            "project_id": project.project_id,
        }

    engine.dispose()


def test_repository_list_helpers_preserve_filtered_results():
    api = repository_backed_api()
    actor = _actor()

    project = api.create_project("Repository Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Primary question",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        status=DatasetStatus.COMMITTED,
        commit_manifest=DatasetCommitManifestInput(
            files=[
                {
                    "checksum": "checksum-1",
                    "path": "capture/data.bin",
                    "size_bytes": 10,
                }
            ]
        ),
        actor=actor,
    )
    created_session = api.create_session(
        project_id=project.project_id,
        session_type=SessionType.OPERATIONAL,
        actor=actor,
    )
    api.register_acquisition_output(
        created_session.session_id,
        file_path="capture/output.bin",
        checksum="abc123",
        size_bytes=12,
        actor=actor,
    )
    created_analysis = api.create_analysis(
        project_id=project.project_id,
        dataset_ids=[dataset.dataset_id],
        method_hash="method-1",
        code_version="code-1",
        status=AnalysisStatus.COMMITTED,
        actor=actor,
    )
    created_claim = api.create_claim(
        project_id=project.project_id,
        statement="Supported claim",
        confidence=0.9,
        status=ClaimStatus.PROPOSED,
        supported_by_dataset_ids=[dataset.dataset_id],
        supported_by_analysis_ids=[created_analysis.analysis_id],
        actor=actor,
    )
    created_visualization = api.create_visualization(
        analysis_id=created_analysis.analysis_id,
        viz_type="heatmap",
        file_path="viz/heatmap.png",
        related_claim_ids=[created_claim.claim_id],
        actor=actor,
    )

    assert api.list_sessions(project_id=project.project_id) == [created_session]
    assert api.list_acquisition_outputs(session_id=created_session.session_id)[0].file_path == (
        "capture/output.bin"
    )
    assert api.list_datasets(project_id=project.project_id) == [dataset]
    assert api.list_analyses(
        project_id=project.project_id,
        dataset_id=dataset.dataset_id,
        question_id=question.question_id,
    ) == [created_analysis]
    assert api.list_claims(
        project_id=project.project_id,
        dataset_id=dataset.dataset_id,
        analysis_id=created_analysis.analysis_id,
    ) == [created_claim]
    assert api.list_visualizations(
        project_id=project.project_id,
        analysis_id=created_analysis.analysis_id,
        claim_id=created_claim.claim_id,
    ) == [created_visualization]


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


def test_fastapi_search_reads_database_changes_after_app_start(monkeypatch, tmp_path):
    db_path = tmp_path / "route-search-refresh.db"
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

    app = create_app()
    _seed_admin(
        app,
        username="route-search-admin",
        password="secret",
    )

    with app.state.db_session_factory() as session:
        project = ProjectModel(name="Inserted for search", description="external")
        session.add(project)
        session.flush()
        question = QuestionModel(
            project_id=project.project_id,
            text="Externally inserted search target",
            question_type="descriptive",
            status="active",
        )
        note = NoteModel(
            project_id=project.project_id,
            raw_content="Externally inserted note search target",
            note_metadata={"owner": "Sam"},
            status="committed",
        )
        session.add(question)
        session.add(note)
        session.commit()
        project_id = project.project_id
        question_id = question.question_id
        note_id = note.note_id

    with TestClient(app) as client:
        login_response = client.post(
            "/auth/login",
            json={
                "username": "route-search-admin",
                "password": "secret",
            },
        )
        assert login_response.status_code == 200
        headers = _auth_headers(login_response.json()["data"]["access_token"])
        search_response = client.get(
            "/search",
            params={"q": "search target", "project_id": project_id},
            headers=headers,
        )

    assert search_response.status_code == 200
    payload = search_response.json()["data"]
    assert {item["question_id"] for item in payload["questions"]} == {str(question_id)}
    assert {item["note_id"] for item in payload["notes"]} == {str(note_id)}


def test_note_transcribed_text_search_survives_app_restart(monkeypatch, tmp_path):
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
            json={"name": "Transcript search"},
            headers=headers,
        ).json()["data"]["project_id"]
        note_response = client.post(
            "/notes",
            json={
                "project_id": project_id,
                "raw_content": "capture log",
                "transcribed_text": "Sam observed stable recordings",
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
            self.question_refactors = other
            self.datasets = other
            self.notes = other
            self.sessions = other
            self.acquisition_outputs = other
            self.analyses = other
            self.claims = other
            self.visualizations = other
            self.graph_change_sets = other

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
