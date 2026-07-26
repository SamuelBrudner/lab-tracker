from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Lock
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.db_models import QuestionModel, QuestionParentModel, QuestionRefactorModel
from lab_tracker.errors import ValidationError
from lab_tracker.models import QuestionStatus
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker.sqlalchemy_repository_parts import repository as repository_module

pytestmark = pytest.mark.postgres


def _create_project(client: TestClient, headers: dict[str, str], name: str) -> UUID:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return UUID(response.json()["data"]["project_id"])


def _create_question(
    client: TestClient,
    headers: dict[str, str],
    project_id: UUID,
    text_value: str,
) -> UUID:
    response = client.post(
        "/questions",
        json={
            "project_id": str(project_id),
            "text": text_value,
            "question_type": "descriptive",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["data"]["question_id"])


def _update_parent(
    client: TestClient,
    headers: dict[str, str],
    question_id: UUID,
    parent_id: UUID,
):
    return client.patch(
        f"/questions/{question_id}",
        json={"parent_question_ids": [str(parent_id)]},
        headers=headers,
    )


def _blocking_pids(client: TestClient, blocked_pid: int) -> list[int]:
    with client.app.state.db_engine.connect() as connection:
        result = connection.scalar(
            text("SELECT pg_blocking_pids(:blocked_pid)"),
            {"blocked_pid": blocked_pid},
        )
    return [int(value) for value in result or []]


def _wait_until_blocked(
    client: TestClient,
    *,
    blocked_pid: int,
    expected_blocker_pid: int,
) -> None:
    deadline = monotonic() + 10
    poll_interval = Event()
    while monotonic() < deadline:
        if expected_blocker_pid in _blocking_pids(client, blocked_pid):
            return
        poll_interval.wait(timeout=0.01)
    pytest.fail(
        f"Backend {blocked_pid} was not blocked by {expected_blocker_pid} before the deadline."
    )


def _backend_pid(repository: SQLAlchemyLabTrackerRepository) -> int:
    value = repository._session.scalar(text("SELECT pg_backend_pid()"))  # noqa: SLF001
    assert value is not None
    return int(value)


def _future_result(future: Future[Any]) -> Any:
    return future.result(timeout=20)


def test_opposite_parent_updates_serialize_and_cannot_commit_a_cycle(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = _create_project(
        postgres_client,
        postgres_admin_auth_headers,
        "Serialized question DAG",
    )
    question_a = _create_question(
        postgres_client,
        postgres_admin_auth_headers,
        project_id,
        "Question A",
    )
    question_b = _create_question(
        postgres_client,
        postgres_admin_auth_headers,
        project_id,
        "Question B",
    )

    original_lock = SQLAlchemyLabTrackerRepository.lock_project_question_dag
    state_lock = Lock()
    first_locked = Event()
    second_entered = Event()
    release_first = Event()
    call_count = 0
    backend_pids: dict[int, int] = {}

    def coordinated_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
    ) -> None:
        nonlocal call_count
        with state_lock:
            call_index = call_count
            call_count += 1
            backend_pids[call_index] = _backend_pid(repository)
        if call_index == 0:
            original_lock(repository, locked_project_id)
            first_locked.set()
            if not release_first.wait(timeout=20):
                raise RuntimeError("Timed out holding the first question-DAG lock.")
            return
        second_entered.set()
        original_lock(repository, locked_project_id)

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_project_question_dag",
        coordinated_lock,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            _update_parent,
            postgres_client,
            postgres_admin_auth_headers,
            question_a,
            question_b,
        )
        assert first_locked.wait(timeout=10)
        second = executor.submit(
            _update_parent,
            postgres_client,
            postgres_admin_auth_headers,
            question_b,
            question_a,
        )
        try:
            assert second_entered.wait(timeout=10)
            assert backend_pids[0] != backend_pids[1]
            _wait_until_blocked(
                postgres_client,
                blocked_pid=backend_pids[1],
                expected_blocker_pid=backend_pids[0],
            )
        finally:
            release_first.set()
        first_response = _future_result(first)
        second_response = _future_result(second)

    assert first_response.status_code == 200, first_response.text
    assert second_response.status_code == 422, second_response.text
    assert second_response.json()["error"]["code"] == "validation_error"

    reloaded_a = postgres_client.get(
        f"/questions/{question_a}",
        headers=postgres_admin_auth_headers,
    )
    reloaded_b = postgres_client.get(
        f"/questions/{question_b}",
        headers=postgres_admin_auth_headers,
    )
    assert reloaded_a.status_code == 200
    assert reloaded_b.status_code == 200
    parent_sets = [
        set(reloaded_a.json()["data"]["parent_question_ids"]),
        set(reloaded_b.json()["data"]["parent_question_ids"]),
    ]
    assert sum(len(parent_ids) for parent_ids in parent_sets) == 1
    assert parent_sets == [{str(question_b)}, set()]

    with postgres_client.app.state.db_session_factory() as session:
        edges = list(
            session.execute(
                select(
                    QuestionParentModel.question_id,
                    QuestionParentModel.parent_question_id,
                )
                .select_from(QuestionParentModel)
                .join(
                    QuestionModel,
                    QuestionModel.question_id == QuestionParentModel.question_id,
                )
                .where(QuestionModel.project_id == str(project_id))
            ).tuples()
        )
    assert edges == [(question_a, question_b)]


def test_disjoint_projects_with_the_same_uuid_prefix_do_not_block(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_project_id = UUID("12345678-0000-4000-8000-000000000001")
    second_project_id = UUID("12345678-0000-4000-8000-000000000002")
    project_ids = iter([first_project_id, second_project_id])
    monkeypatch.setattr(
        "lab_tracker.services.project_service.uuid4",
        lambda: next(project_ids),
    )
    assert (
        _create_project(postgres_client, postgres_admin_auth_headers, "Prefix project one")
        == first_project_id
    )
    assert (
        _create_project(postgres_client, postgres_admin_auth_headers, "Prefix project two")
        == second_project_id
    )
    first_question = _create_question(
        postgres_client,
        postgres_admin_auth_headers,
        first_project_id,
        "First independent question",
    )
    first_parent = _create_question(
        postgres_client,
        postgres_admin_auth_headers,
        first_project_id,
        "First independent parent",
    )
    second_question = _create_question(
        postgres_client,
        postgres_admin_auth_headers,
        second_project_id,
        "Second independent question",
    )
    second_parent = _create_question(
        postgres_client,
        postgres_admin_auth_headers,
        second_project_id,
        "Second independent parent",
    )

    original_lock = SQLAlchemyLabTrackerRepository.lock_project_question_dag
    first_locked = Event()
    second_locked = Event()
    release_first = Event()

    def hold_first_project(
        repository: SQLAlchemyLabTrackerRepository,
        project_id: UUID,
    ) -> None:
        original_lock(repository, project_id)
        if project_id == first_project_id:
            first_locked.set()
            if not release_first.wait(timeout=20):
                raise RuntimeError("Timed out holding the first project's DAG lock.")
        elif project_id == second_project_id:
            second_locked.set()

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_project_question_dag",
        hold_first_project,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            _update_parent,
            postgres_client,
            postgres_admin_auth_headers,
            first_question,
            first_parent,
        )
        assert first_locked.wait(timeout=10)
        second = executor.submit(
            _update_parent,
            postgres_client,
            postgres_admin_auth_headers,
            second_question,
            second_parent,
        )
        try:
            assert second_locked.wait(timeout=10)
            second_response = _future_result(second)
        finally:
            release_first.set()
        first_response = _future_result(first)

    assert first_response.status_code == 200, first_response.text
    assert second_response.status_code == 200, second_response.text


def test_text_update_waits_for_parent_update_and_preserves_the_winning_edge(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = _create_project(
        postgres_client,
        postgres_admin_auth_headers,
        "Full-snapshot question serialization",
    )
    question_id = _create_question(
        postgres_client,
        postgres_admin_auth_headers,
        project_id,
        "Original text",
    )
    parent_id = _create_question(
        postgres_client,
        postgres_admin_auth_headers,
        project_id,
        "Parent question",
    )

    original_lock = SQLAlchemyLabTrackerRepository.lock_project_question_dag
    state_lock = Lock()
    first_locked = Event()
    second_entered = Event()
    release_first = Event()
    call_count = 0
    backend_pids: dict[int, int] = {}

    def coordinated_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
    ) -> None:
        nonlocal call_count
        with state_lock:
            call_index = call_count
            call_count += 1
            backend_pids[call_index] = _backend_pid(repository)
        if call_index == 0:
            original_lock(repository, locked_project_id)
            first_locked.set()
            if not release_first.wait(timeout=20):
                raise RuntimeError("Timed out holding the parent-update DAG lock.")
            return
        second_entered.set()
        original_lock(repository, locked_project_id)

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_project_question_dag",
        coordinated_lock,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        parent_update = executor.submit(
            _update_parent,
            postgres_client,
            postgres_admin_auth_headers,
            question_id,
            parent_id,
        )
        assert first_locked.wait(timeout=10)
        text_update = executor.submit(
            postgres_client.patch,
            f"/questions/{question_id}",
            json={"text": "Text updated after the edge"},
            headers=postgres_admin_auth_headers,
        )
        try:
            assert second_entered.wait(timeout=10)
            assert backend_pids[0] != backend_pids[1]
            _wait_until_blocked(
                postgres_client,
                blocked_pid=backend_pids[1],
                expected_blocker_pid=backend_pids[0],
            )
        finally:
            release_first.set()
        parent_response = _future_result(parent_update)
        text_response = _future_result(text_update)

    assert parent_response.status_code == 200, parent_response.text
    assert text_response.status_code == 200, text_response.text
    reloaded = postgres_client.get(
        f"/questions/{question_id}",
        headers=postgres_admin_auth_headers,
    )
    assert reloaded.status_code == 200
    assert reloaded.json()["data"]["text"] == "Text updated after the edge"
    assert reloaded.json()["data"]["parent_question_ids"] == [str(parent_id)]


def test_direct_validation_failure_rolls_back_and_releases_the_project_lock(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_project = _create_project(
        postgres_client,
        postgres_admin_auth_headers,
        "Validation rollback project",
    )
    second_project = _create_project(
        postgres_client,
        postgres_admin_auth_headers,
        "Cross-project parent",
    )
    question = _create_question(
        postgres_client,
        postgres_admin_auth_headers,
        first_project,
        "Question with invalid parent",
    )
    cross_project_parent = _create_question(
        postgres_client,
        postgres_admin_auth_headers,
        second_project,
        "Invalid parent",
    )

    original_lock = SQLAlchemyLabTrackerRepository.lock_project_question_dag
    locked_projects: list[UUID] = []

    def record_lock(
        repository: SQLAlchemyLabTrackerRepository,
        project_id: UUID,
    ) -> None:
        original_lock(repository, project_id)
        locked_projects.append(project_id)

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_project_question_dag",
        record_lock,
    )

    with postgres_client.app.state.db_session_factory() as session:
        api = LabTrackerAPI(repository=SQLAlchemyLabTrackerRepository(session))
        actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
        with pytest.raises(ValidationError, match="same project"):
            api.update_question(
                question,
                parent_question_ids=[cross_project_parent],
                actor=actor,
            )

        assert not session.in_transaction()
        assert locked_projects == [first_project]

    lock_key = repository_module._project_question_dag_lock_key(first_project)  # noqa: SLF001
    with postgres_client.app.state.db_engine.begin() as connection:
        acquired = connection.scalar(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )
        assert acquired is True


def test_refactor_command_uses_the_same_project_dag_lock(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = _create_project(
        postgres_client,
        postgres_admin_auth_headers,
        "Refactor lock project",
    )
    source_id = _create_question(
        postgres_client,
        postgres_admin_auth_headers,
        project_id,
        "Refactor source",
    )

    original_lock = SQLAlchemyLabTrackerRepository.lock_project_question_dag
    state_lock = Lock()
    first_locked = Event()
    second_entered = Event()
    release_first = Event()
    call_count = 0
    backend_pids: dict[int, int] = {}

    def coordinated_refactor_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
    ) -> None:
        nonlocal call_count
        with state_lock:
            call_index = call_count
            call_count += 1
            backend_pids[call_index] = _backend_pid(repository)
        if call_index == 0:
            original_lock(repository, locked_project_id)
            first_locked.set()
            if not release_first.wait(timeout=20):
                raise RuntimeError("Timed out holding the first refactor DAG lock.")
            return
        second_entered.set()
        original_lock(repository, locked_project_id)

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_project_question_dag",
        coordinated_refactor_lock,
    )

    def refactor(replacement_text: str):
        return postgres_client.post(
            f"/questions/{source_id}/refactor",
            json={
                "replacement": {
                    "text": replacement_text,
                    "question_type": "descriptive",
                    "status": QuestionStatus.STAGED.value,
                },
                "reason": "Verify serialization",
            },
            headers=postgres_admin_auth_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(refactor, "First refactored replacement")
        assert first_locked.wait(timeout=10)
        second = executor.submit(refactor, "Second refactored replacement")
        try:
            assert second_entered.wait(timeout=10)
            assert backend_pids[0] != backend_pids[1]
            _wait_until_blocked(
                postgres_client,
                blocked_pid=backend_pids[1],
                expected_blocker_pid=backend_pids[0],
            )
        finally:
            release_first.set()
        first_response = _future_result(first)
        second_response = _future_result(second)

    assert first_response.status_code == 201, first_response.text
    assert second_response.status_code == 422, second_response.text
    assert second_response.json()["error"]["code"] == "validation_error"

    with postgres_client.app.state.db_session_factory() as session:
        refactor_count = session.scalar(
            select(func.count())
            .select_from(QuestionRefactorModel)
            .where(QuestionRefactorModel.source_question_id == str(source_id))
        )
    assert refactor_count == 1
