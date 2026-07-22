"""Atomicity regressions for direct/background application commands."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from api_helpers import repository_backed_api
from fastapi.testclient import TestClient

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.db_models import UserModel
from lab_tracker.errors import LabTrackerError
from lab_tracker.models import (
    AcceptanceMode,
    AnalysisStatus,
    ClaimInput,
    ClaimStatus,
    DatasetCommitManifestInput,
    DatasetFile,
    DatasetStatus,
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    QuestionStatus,
    QuestionType,
    VisualizationInput,
)
from lab_tracker.services.project_service import ProjectService
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


def _actor(role: Role = Role.ADMIN) -> AuthContext:
    return AuthContext(user_id=uuid4(), role=role)


def _registered_editor(api) -> AuthContext:  # noqa: ANN001
    actor = _actor(Role.EDITOR)
    _, session = api._test_resources  # type: ignore[attr-defined]
    session.add(
        UserModel(
            user_id=str(actor.user_id),
            username=f"editor-{actor.user_id.hex}",
            password_hash="unused",
            role=actor.role.value,
        )
    )
    session.commit()
    return actor


def _staged_analysis(api, actor):  # noqa: ANN001
    project = api.create_project("Atomicity Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Is the signal stable?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        commit_manifest=DatasetCommitManifestInput(
            files=[DatasetFile(path="data.csv", checksum="abc123")]
        ),
        status=DatasetStatus.COMMITTED,
        actor=actor,
    )
    analysis = api.create_analysis(
        project_id=project.project_id,
        dataset_ids=[dataset.dataset_id],
        method_hash="method-1",
        code_version="v1",
        actor=actor,
    )
    return project, question, analysis


def _invalid_graph_change_set(project_id: UUID, note_id: UUID) -> GraphChangeSet:
    """Build a valid-first, invalid-second accepted operation sequence."""

    change_set_id = uuid4()
    valid_question = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=change_set_id,
        sequence=0,
        op=GraphChangeOp.CREATE,
        entity_type=EntityType.QUESTION,
        payload={
            "project_id": str(project_id),
            "text": "Does the whiteboard protocol improve yield?",
            "question_type": "descriptive",
        },
        client_ref="q1",
        status=GraphChangeOperationStatus.ACCEPTED,
        acceptance_mode=AcceptanceMode.BULK_ACCEPTED,
    )
    missing_question_dataset = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=change_set_id,
        sequence=1,
        op=GraphChangeOp.CREATE,
        entity_type=EntityType.DATASET,
        payload={
            "project_id": str(project_id),
            "primary_question_id": str(uuid4()),
        },
        client_ref="d1",
        status=GraphChangeOperationStatus.ACCEPTED,
        acceptance_mode=AcceptanceMode.BULK_ACCEPTED,
    )
    return GraphChangeSet(
        change_set_id=change_set_id,
        project_id=project_id,
        source_note_id=note_id,
        model="test-model",
        prompt_version="v1",
        status=GraphChangeSetStatus.READY,
        operations=[valid_question, missing_question_dataset],
    )


def _seed_direct_invalid_graph(api, actor):  # noqa: ANN001
    project = api.create_project("Graph Atomicity", actor=actor)
    note = api.create_note(
        project_id=project.project_id,
        raw_content="Whiteboard notes",
        actor=actor,
    )
    change_set = _invalid_graph_change_set(project.project_id, note.note_id)
    api.graph_drafts.repository.graph_change_sets.save(change_set)
    api.graph_drafts.repository.commit()
    return project, change_set


def test_commit_analysis_rolls_back_all_components_via_direct_facade() -> None:
    api = repository_backed_api()
    actor = _actor()
    project, question, analysis = _staged_analysis(api, actor)

    with pytest.raises(LabTrackerError):
        api.commit_analysis(
            analysis.analysis_id,
            environment_hash="env-1",
            claims=[
                ClaimInput(
                    statement="Signal is stable",
                    confidence=0.8,
                    status=ClaimStatus.SUPPORTED,
                    answers_question_ids=[question.question_id],
                ),
                ClaimInput(
                    statement="References a missing question",
                    confidence=0.5,
                    answers_question_ids=[uuid4()],
                ),
            ],
            visualizations=[
                VisualizationInput(viz_type="line", file_path="figs/signal.png")
            ],
            actor=actor,
        )

    reloaded = api.get_analysis(analysis.analysis_id)
    assert reloaded.status == AnalysisStatus.STAGED
    assert reloaded.environment_hash is None
    assert api.list_claims(project_id=project.project_id) == []
    assert api.list_visualizations(analysis_id=analysis.analysis_id) == []


def test_commit_graph_change_set_rolls_back_all_components_via_direct_facade() -> None:
    api = repository_backed_api()
    actor = _actor()
    project, change_set = _seed_direct_invalid_graph(api, actor)

    with pytest.raises(LabTrackerError):
        api.commit_graph_change_set(
            change_set.change_set_id,
            message="apply",
            actor=actor,
        )

    assert api.list_questions(project_id=project.project_id, search="whiteboard") == []
    reloaded = api.get_graph_change_set(change_set.change_set_id)
    assert reloaded.status == GraphChangeSetStatus.READY
    assert all(
        operation.status == GraphChangeOperationStatus.ACCEPTED
        for operation in reloaded.operations
    )


def test_nested_application_transaction_commits_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Single commit", actor=actor)
    repository = api.questions.repository
    original_commit = repository.commit
    commit_calls = 0

    def counted_commit() -> None:
        nonlocal commit_calls
        commit_calls += 1
        original_commit()

    monkeypatch.setattr(repository, "commit", counted_commit)
    with api.questions.application_transaction():
        api.questions.create_question(
            project_id=project.project_id,
            text="Outer question",
            question_type=QuestionType.DESCRIPTIVE,
            actor=actor,
        )
        with api.questions.application_transaction():
            api.questions.create_question(
                project_id=project.project_id,
                text="Inner question",
                question_type=QuestionType.DESCRIPTIVE,
                actor=actor,
            )

    assert commit_calls == 1
    assert len(api.list_questions(project_id=project.project_id)) == 2


def test_commit_failure_rolls_back_hooks_and_leaves_boundary_reusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Commit failure", actor=actor)
    context = api.questions._context  # noqa: SLF001
    repository = api.questions.repository
    original_commit = repository.commit
    commit_attempts = 0
    events: list[str] = []

    def fail_once_then_commit() -> None:
        nonlocal commit_attempts
        commit_attempts += 1
        if commit_attempts == 1:
            raise RuntimeError("simulated commit failure")
        original_commit()

    monkeypatch.setattr(repository, "commit", fail_once_then_commit)

    with (
        pytest.raises(RuntimeError, match="simulated commit failure"),
        context.application_transaction(),
    ):
        api.questions.create_question(
            project_id=project.project_id,
            text="Must roll back",
            question_type=QuestionType.DESCRIPTIVE,
            actor=actor,
        )
        api.questions.run_after_commit(lambda: events.append("wrong commit"))
        api.questions.run_after_rollback(lambda: events.append("rolled back"))

    assert events == ["rolled back"]
    assert not context.transaction.active
    assert context.transaction.after_commit_actions == []
    assert context.transaction.after_rollback_actions == []
    assert api.list_questions(project_id=project.project_id) == []

    with context.application_transaction():
        api.questions.create_question(
            project_id=project.project_id,
            text="Persists after reset",
            question_type=QuestionType.DESCRIPTIVE,
            actor=actor,
        )
        api.questions.run_after_commit(lambda: events.append("committed"))

    assert commit_attempts == 2
    assert events == ["rolled back", "committed"]
    assert [
        question.text for question in api.list_questions(project_id=project.project_id)
    ] == ["Persists after reset"]


@pytest.mark.parametrize("outcome", ["commit", "rollback"])
def test_deferred_hook_failures_log_and_continue(
    outcome: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    events: list[str] = []
    warning_calls: list[tuple[str, bool]] = []

    def record_warning(message: str, *args: object, **kwargs: object) -> None:
        warning_calls.append((message % args, kwargs.get("exc_info") is True))

    monkeypatch.setattr(
        "lab_tracker.services.base._logger.warning",
        record_warning,
    )

    def fail_hook() -> None:
        raise RuntimeError(f"{outcome} hook failed")

    register: Callable[[Callable[[], None]], None]
    register = (
        api.questions.run_after_commit
        if outcome == "commit"
        else api.questions.run_after_rollback
    )
    if outcome == "commit":
        with api.questions.application_transaction():
            register(fail_hook)
            register(lambda: events.append("continued"))
    else:
        with (
            pytest.raises(RuntimeError, match="abort transaction"),
            api.questions.application_transaction(),
        ):
            register(fail_hook)
            register(lambda: events.append("continued"))
            raise RuntimeError("abort transaction")

    assert events == ["continued"]
    assert warning_calls == [
        (f"Deferred after_{outcome} action failed: {outcome} hook failed", True)
    ]


@pytest.mark.parametrize("client_capture_id", [None, "keyed-orphan-prevention"])
def test_project_and_owner_membership_roll_back_together(
    client_capture_id: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = _registered_editor(api)

    def fail_membership_save(_membership) -> None:  # noqa: ANN001
        raise RuntimeError("membership insert failed")

    monkeypatch.setattr(
        api.projects.repository.project_memberships,
        "save",
        fail_membership_save,
    )

    with pytest.raises(RuntimeError, match="membership insert failed"):
        api.projects.create_project_result(
            "Orphan prevention",
            client_capture_id=client_capture_id,
            actor=actor,
        )

    assert api.projects.repository.projects.list() == []
    assert api.projects.list_project_memberships(user_id=actor.user_id) == []


def test_group_and_owner_membership_roll_back_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = _registered_editor(api)

    def fail_membership_save(_membership) -> None:  # noqa: ANN001
        raise RuntimeError("group membership insert failed")

    monkeypatch.setattr(
        api.projects.repository.group_memberships,
        "save",
        fail_membership_save,
    )

    with pytest.raises(RuntimeError, match="group membership insert failed"):
        api.projects.create_project_group("Orphan prevention", actor=actor)

    assert api.projects.repository.project_groups.list() == []
    assert api.projects.list_group_memberships(user_id=actor.user_id, actor=actor) == []


def test_project_capture_race_recovery_keeps_typed_result_and_one_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = _registered_editor(api)
    first = api.projects.create_project_result(
        "Idempotent atomic project",
        client_capture_id="atomic-race-key",
        actor=actor,
    )
    original_lookup = ProjectService._find_client_capture_project
    lookup_calls = 0

    def miss_once(self, client_capture_id, *, created_by):  # noqa: ANN001
        nonlocal lookup_calls
        lookup_calls += 1
        if lookup_calls == 1:
            return None
        return original_lookup(self, client_capture_id, created_by=created_by)

    monkeypatch.setattr(ProjectService, "_find_client_capture_project", miss_once)
    replay = api.projects.create_project_result(
        "Idempotent atomic project",
        client_capture_id="atomic-race-key",
        actor=actor,
    )

    assert first.created
    assert replay.reused
    assert replay.entity.project_id == first.entity.project_id
    assert lookup_calls == 2
    memberships = api.projects.list_project_memberships(
        project_id=first.entity.project_id,
        user_id=actor.user_id,
    )
    assert len(memberships) == 1


def test_project_capture_collision_savepoint_preserves_outer_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = _actor()
    existing = api.projects.create_project_result(
        "Savepoint replay project",
        client_capture_id="savepoint-replay-key",
        actor=actor,
    )
    original_lookup = ProjectService._find_client_capture_project
    repository = api.projects.repository
    original_rollback = repository.rollback
    lookup_calls = 0
    full_rollbacks = 0

    def miss_once(self, client_capture_id, *, created_by):  # noqa: ANN001
        nonlocal lookup_calls
        lookup_calls += 1
        if lookup_calls == 1:
            return None
        return original_lookup(self, client_capture_id, created_by=created_by)

    def counted_rollback() -> None:
        nonlocal full_rollbacks
        full_rollbacks += 1
        original_rollback()

    monkeypatch.setattr(ProjectService, "_find_client_capture_project", miss_once)
    monkeypatch.setattr(repository, "rollback", counted_rollback)

    with api.questions.application_transaction():
        before = api.questions.create_question(
            project_id=existing.entity.project_id,
            text="Before recoverable collision",
            question_type=QuestionType.DESCRIPTIVE,
            actor=actor,
        )
        replay = api.projects.create_project_result(
            "Savepoint replay project",
            client_capture_id="savepoint-replay-key",
            actor=actor,
        )
        after = api.questions.create_question(
            project_id=existing.entity.project_id,
            text="After recoverable collision",
            question_type=QuestionType.DESCRIPTIVE,
            actor=actor,
        )

    assert replay.reused
    assert replay.entity.project_id == existing.entity.project_id
    assert lookup_calls == 2
    assert full_rollbacks == 0
    persisted = api.list_questions(project_id=existing.entity.project_id)
    assert {question.question_id for question in persisted} == {
        before.question_id,
        after.question_id,
    }


def _assert_http_graph_commit_rolls_back(
    client: TestClient,
    headers: dict[str, str],
) -> None:
    project_response = client.post(
        "/projects",
        json={"name": "HTTP graph atomicity"},
        headers=headers,
    )
    assert project_response.status_code == 201, project_response.text
    project_id = UUID(project_response.json()["data"]["project_id"])
    note_response = client.post(
        "/notes",
        json={"project_id": str(project_id), "raw_content": "Whiteboard notes"},
        headers=headers,
    )
    assert note_response.status_code == 201, note_response.text
    note_id = UUID(note_response.json()["data"]["note_id"])
    change_set = _invalid_graph_change_set(project_id, note_id)
    with client.app.state.db_session_factory() as session:
        repository = SQLAlchemyLabTrackerRepository(session)
        repository.graph_change_sets.save(change_set)
        repository.commit()

    commit = client.post(
        f"/graph-drafts/{change_set.change_set_id}/commit",
        json={"message": "must be atomic"},
        headers=headers,
    )
    assert commit.status_code == 404, commit.text

    questions = client.get(
        f"/questions?project_id={project_id}&search=whiteboard&limit=50&offset=0",
        headers=headers,
    )
    assert questions.status_code == 200, questions.text
    assert questions.json()["data"] == []
    reloaded = client.get(
        f"/graph-drafts/{change_set.change_set_id}",
        headers=headers,
    )
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["data"]["status"] == GraphChangeSetStatus.READY.value


def test_http_graph_commit_rolls_back_request_transaction(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    _assert_http_graph_commit_rolls_back(client, admin_auth_headers)


@pytest.mark.postgres
def test_postgres_direct_graph_commit_rolls_back_application_transaction(
    postgres_client: TestClient,
) -> None:
    with postgres_client.app.state.db_session_factory() as session:
        api = LabTrackerAPI(repository=SQLAlchemyLabTrackerRepository(session))
        actor = _actor()
        project, change_set = _seed_direct_invalid_graph(api, actor)

        with pytest.raises(LabTrackerError):
            api.commit_graph_change_set(
                change_set.change_set_id,
                message="must be atomic",
                actor=actor,
            )

        assert api.list_questions(project_id=project.project_id, search="whiteboard") == []
        reloaded = api.get_graph_change_set(change_set.change_set_id)
        assert reloaded.status == GraphChangeSetStatus.READY
