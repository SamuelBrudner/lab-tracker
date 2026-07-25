"""Real PostgreSQL coverage for acquisition/promotion serialization."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Lock
from time import monotonic
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

pytestmark = pytest.mark.postgres


def _create_project_question_session(
    client: TestClient,
    headers: dict[str, str],
    *,
    label: str,
) -> tuple[UUID, UUID, UUID]:
    project_response = client.post(
        "/projects",
        json={"name": f"{label} project"},
        headers=headers,
    )
    assert project_response.status_code == 201, project_response.text
    project_id = UUID(project_response.json()["data"]["project_id"])
    question_response = client.post(
        "/questions",
        json={
            "project_id": str(project_id),
            "text": f"{label} question",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    )
    assert question_response.status_code == 201, question_response.text
    question_id = UUID(question_response.json()["data"]["question_id"])
    session_response = client.post(
        "/sessions",
        json={
            "project_id": str(project_id),
            "session_type": "operational",
        },
        headers=headers,
    )
    assert session_response.status_code == 201, session_response.text
    session_id = UUID(session_response.json()["data"]["session_id"])
    return project_id, question_id, session_id


def _backend_pid(repository: SQLAlchemyLabTrackerRepository) -> int:
    value = repository._session.scalar(text("SELECT pg_backend_pid()"))  # noqa: SLF001
    assert value is not None
    return int(value)


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
        f"Backend {blocked_pid} was not blocked by {expected_blocker_pid} "
        "before the deadline."
    )


def _future_result(future: Future[Any]) -> Any:
    return future.result(timeout=20)


def _install_two_request_lock_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Event, Event, Event, dict[int, int]]:
    original_lock = SQLAlchemyLabTrackerRepository.lock_session_acquisition_state
    state_lock = Lock()
    first_locked = Event()
    second_entered = Event()
    release_first = Event()
    backend_pids: dict[int, int] = {}
    call_count = 0

    def coordinated_lock(
        repository: SQLAlchemyLabTrackerRepository,
        session_id: UUID,
    ) -> None:
        nonlocal call_count
        with state_lock:
            call_index = call_count
            call_count += 1
            backend_pids[call_index] = _backend_pid(repository)
        if call_index == 0:
            original_lock(repository, session_id)
            first_locked.set()
            if not release_first.wait(timeout=20):
                raise RuntimeError("Timed out holding the first Session-state lock.")
            return
        second_entered.set()
        original_lock(repository, session_id)

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_session_acquisition_state",
        coordinated_lock,
    )
    return first_locked, second_entered, release_first, backend_pids


def _install_two_request_experiment_lock_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Event, Event, Event, dict[int, int]]:
    original_lock = SQLAlchemyLabTrackerRepository.lock_experiment_updates
    state_lock = Lock()
    first_locked = Event()
    second_entered = Event()
    release_first = Event()
    backend_pids: dict[int, int] = {}
    call_count = 0

    def coordinated_lock(
        repository: SQLAlchemyLabTrackerRepository,
        experiment_ids,
    ) -> None:
        nonlocal call_count
        resolved_ids = tuple(experiment_ids)
        with state_lock:
            call_index = call_count
            call_count += 1
            backend_pids[call_index] = _backend_pid(repository)
        if call_index == 0:
            original_lock(repository, resolved_ids)
            first_locked.set()
            if not release_first.wait(timeout=20):
                raise RuntimeError("Timed out holding the first Experiment lock.")
            return
        second_entered.set()
        original_lock(repository, resolved_ids)

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_experiment_updates",
        coordinated_lock,
    )
    return first_locked, second_entered, release_first, backend_pids


def test_collection_capture_commits_before_waiting_promotion_snapshots_state(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, question_id, session_id = _create_project_question_session(
        postgres_client,
        postgres_admin_auth_headers,
        label="Capture versus promotion",
    )
    first_locked, second_entered, release_first, backend_pids = (
        _install_two_request_lock_barrier(monkeypatch)
    )

    def capture():
        return postgres_client.post(
            f"/sessions/{session_id}/collections/trials/snapshots",
            json={
                "client_capture_id": "concurrent-capture",
                "observed_at": "2026-07-24T12:00:00Z",
                "complete": True,
                "manifest": {
                    "schema_version": 1,
                    "members": [
                        {
                            "path": "trial-0001/data.bin",
                            "checksum": "a" * 64,
                            "size_bytes": 1,
                        }
                    ],
                },
            },
            headers=postgres_admin_auth_headers,
        )

    def promote():
        return postgres_client.post(
            f"/sessions/{session_id}/promote-to-dataset",
            json={"primary_question_id": str(question_id)},
            headers=postgres_admin_auth_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        capture_future = executor.submit(capture)
        assert first_locked.wait(timeout=10)
        promotion_future = executor.submit(promote)
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
        capture_response = _future_result(capture_future)
        promotion_response = _future_result(promotion_future)

    assert capture_response.status_code == 201, capture_response.text
    assert promotion_response.status_code == 201, promotion_response.text
    snapshot_id = capture_response.json()["data"]["snapshot_id"]
    references = promotion_response.json()["data"]["commit_manifest"][
        "collection_snapshots"
    ]
    assert [reference["snapshot_id"] for reference in references] == [snapshot_id]


def test_experiment_membership_commits_before_waiting_promotion_inherits_it(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, question_id, session_id = _create_project_question_session(
        postgres_client,
        postgres_admin_auth_headers,
        label="Membership versus promotion",
    )
    experiment_response = postgres_client.post(
        "/experiments",
        json={
            "project_id": str(project_id),
            "name": "Inherited under lock",
            "primary_question_id": str(question_id),
        },
        headers=postgres_admin_auth_headers,
    )
    assert experiment_response.status_code == 201, experiment_response.text
    experiment_id = UUID(experiment_response.json()["data"]["experiment_id"])
    output_response = postgres_client.post(
        f"/sessions/{session_id}/outputs",
        json={
            "file_path": "run/output.bin",
            "checksum": "sha256:promotion-lock",
            "size_bytes": 1,
        },
        headers=postgres_admin_auth_headers,
    )
    assert output_response.status_code == 201, output_response.text
    first_locked, second_entered, release_first, backend_pids = (
        _install_two_request_lock_barrier(monkeypatch)
    )

    def add_membership():
        return postgres_client.put(
            f"/experiments/{experiment_id}/sessions/{session_id}",
            headers=postgres_admin_auth_headers,
        )

    def promote():
        return postgres_client.post(
            f"/sessions/{session_id}/promote-to-dataset",
            json={"primary_question_id": str(question_id)},
            headers=postgres_admin_auth_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        membership_future = executor.submit(add_membership)
        assert first_locked.wait(timeout=10)
        promotion_future = executor.submit(promote)
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
        membership_response = _future_result(membership_future)
        promotion_response = _future_result(promotion_future)

    assert membership_response.status_code == 200, membership_response.text
    assert promotion_response.status_code == 201, promotion_response.text
    dataset_id = promotion_response.json()["data"]["dataset_id"]
    experiment_datasets = postgres_client.get(
        f"/experiments/{experiment_id}/datasets",
        headers=postgres_admin_auth_headers,
    )
    assert experiment_datasets.status_code == 200, experiment_datasets.text
    assert [
        dataset["dataset_id"] for dataset in experiment_datasets.json()["data"]
    ] == [dataset_id]


def test_archive_commits_before_waiting_promotion_rejects_inheritance(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, question_id, session_id = _create_project_question_session(
        postgres_client,
        postgres_admin_auth_headers,
        label="Archive versus promotion",
    )
    experiment_response = postgres_client.post(
        "/experiments",
        json={
            "project_id": str(project_id),
            "name": "Archive winner",
            "primary_question_id": str(question_id),
        },
        headers=postgres_admin_auth_headers,
    )
    assert experiment_response.status_code == 201, experiment_response.text
    experiment_id = UUID(experiment_response.json()["data"]["experiment_id"])
    membership_response = postgres_client.put(
        f"/experiments/{experiment_id}/sessions/{session_id}",
        headers=postgres_admin_auth_headers,
    )
    assert membership_response.status_code == 200, membership_response.text
    close_response = postgres_client.patch(
        f"/experiments/{experiment_id}",
        json={"status": "closed"},
        headers=postgres_admin_auth_headers,
    )
    assert close_response.status_code == 200, close_response.text
    output_response = postgres_client.post(
        f"/sessions/{session_id}/outputs",
        json={
            "file_path": "run/archive-race.bin",
            "checksum": "sha256:archive-race",
            "size_bytes": 1,
        },
        headers=postgres_admin_auth_headers,
    )
    assert output_response.status_code == 201, output_response.text
    first_locked, second_entered, release_first, backend_pids = (
        _install_two_request_experiment_lock_barrier(monkeypatch)
    )

    def archive():
        return postgres_client.patch(
            f"/experiments/{experiment_id}",
            json={"status": "archived"},
            headers=postgres_admin_auth_headers,
        )

    def promote():
        return postgres_client.post(
            f"/sessions/{session_id}/promote-to-dataset",
            json={"primary_question_id": str(question_id)},
            headers=postgres_admin_auth_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        archive_future = executor.submit(archive)
        assert first_locked.wait(timeout=10)
        promotion_future = executor.submit(promote)
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
        archive_response = _future_result(archive_future)
        promotion_response = _future_result(promotion_future)

    assert archive_response.status_code == 200, archive_response.text
    assert archive_response.json()["data"]["status"] == "archived"
    assert promotion_response.status_code == 422, promotion_response.text
    assert "archived Experiments" in promotion_response.json()["error"]["message"]
    experiment_datasets = postgres_client.get(
        f"/experiments/{experiment_id}/datasets",
        headers=postgres_admin_auth_headers,
    )
    assert experiment_datasets.status_code == 200, experiment_datasets.text
    assert experiment_datasets.json()["data"] == []
