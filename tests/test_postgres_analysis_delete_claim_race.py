"""PostgreSQL races between analysis deletion and claim promotion (M50).

Analysis deletion lets a proposed claim lose its analysis support link, and
promoting a claim to supported requires evidence. Both run their check and
write under ``lock_project_references``, so whichever commits second observes
the first: a supported claim can never be left without evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Lock
from time import monotonic
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from lab_tracker.db_models import AnalysisModel, ClaimAnalysisModel, ClaimModel
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

pytestmark = pytest.mark.postgres


def _post(client: TestClient, headers: dict[str, str], path: str, payload: dict[str, Any]):
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _proposed_claim_on_analysis(
    client: TestClient,
    headers: dict[str, str],
    name: str,
) -> tuple[str, str]:
    project_id = _post(client, headers, "/projects", {"name": name})["project_id"]
    question_id = _post(
        client,
        headers,
        "/questions",
        {
            "project_id": project_id,
            "text": "Is the analysis still evidence?",
            "question_type": "descriptive",
            "status": "active",
        },
    )["question_id"]
    dataset_id = _post(
        client,
        headers,
        "/datasets",
        {"project_id": project_id, "primary_question_id": question_id},
    )["dataset_id"]
    analysis_id = _post(
        client,
        headers,
        "/analyses",
        {
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "claim-race-method",
            "code_version": "claim-race-code",
        },
    )["analysis_id"]
    claim = _post(
        client,
        headers,
        "/claims",
        {
            "project_id": project_id,
            "statement": "The analysis supports this.",
            "confidence": 0.6,
            "supported_by_analysis_ids": [analysis_id],
        },
    )
    assert claim["status"] == "proposed"
    return analysis_id, claim["claim_id"]


def _backend_pid(repository: SQLAlchemyLabTrackerRepository) -> int:
    value = repository._session.scalar(text("SELECT pg_backend_pid()"))  # noqa: SLF001
    assert value is not None
    return int(value)


def _wait_until_blocked(client: TestClient, *, blocked_pid: int, blocker_pid: int) -> None:
    deadline = monotonic() + 10
    poll = Event()
    while monotonic() < deadline:
        with client.app.state.db_engine.connect() as connection:
            blockers = connection.scalar(
                text("SELECT pg_blocking_pids(:blocked_pid)"),
                {"blocked_pid": blocked_pid},
            )
        if blocker_pid in [int(value) for value in blockers or []]:
            return
        poll.wait(timeout=0.01)
    pytest.fail(f"Backend {blocked_pid} was not blocked by {blocker_pid} before the deadline.")


def _race(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    first: Callable[[], Any],
    second: Callable[[], Any],
) -> tuple[Any, Any]:
    """Run ``first`` holding the project reference lock, then ``second`` behind it."""

    original_lock = SQLAlchemyLabTrackerRepository.lock_project_references
    state_lock = Lock()
    first_locked = Event()
    second_entered = Event()
    release_first = Event()
    call_count = 0
    backend_pids: dict[int, int] = {}

    def coordinated_lock(repository: SQLAlchemyLabTrackerRepository, project_id: UUID) -> None:
        nonlocal call_count
        with state_lock:
            call_index = call_count
            call_count += 1
            if call_index < 2:
                backend_pids[call_index] = _backend_pid(repository)
        if call_index == 0:
            original_lock(repository, project_id)
            first_locked.set()
            if not release_first.wait(timeout=20):
                raise RuntimeError("Timed out holding the first reference lock.")
            return
        if call_index == 1:
            second_entered.set()
        original_lock(repository, project_id)

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_project_references",
        coordinated_lock,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future: Future[Any] = executor.submit(first)
        assert first_locked.wait(timeout=10)
        second_future: Future[Any] = executor.submit(second)
        try:
            assert second_entered.wait(timeout=10)
            assert backend_pids[0] != backend_pids[1]
            _wait_until_blocked(
                client,
                blocked_pid=backend_pids[1],
                blocker_pid=backend_pids[0],
            )
        finally:
            release_first.set()
        return first_future.result(timeout=20), second_future.result(timeout=20)


def _promote(client: TestClient, headers: dict[str, str], claim_id: str):
    return client.patch(f"/claims/{claim_id}", json={"status": "supported"}, headers=headers)


def test_analysis_delete_first_makes_concurrent_promotion_fail_for_lack_of_evidence(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = postgres_client
    headers = postgres_admin_auth_headers
    analysis_id, claim_id = _proposed_claim_on_analysis(client, headers, "Delete wins")

    deleted, promoted = _race(
        client,
        monkeypatch,
        lambda: client.delete(f"/analyses/{analysis_id}", headers=headers),
        lambda: _promote(client, headers, claim_id),
    )

    assert deleted.status_code == 200, deleted.text
    assert promoted.status_code == 422, promoted.text
    assert promoted.json()["error"]["message"] == (
        "Supported claims require supporting datasets or analyses."
    )
    with client.app.state.db_session_factory() as session:
        assert session.get(AnalysisModel, analysis_id) is None
        claim = session.get(ClaimModel, claim_id)
        assert claim is not None
        assert claim.status == "proposed"
        assert session.scalar(select(func.count()).select_from(ClaimAnalysisModel)) == 0


def test_claim_promotion_first_blocks_concurrent_analysis_delete(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = postgres_client
    headers = postgres_admin_auth_headers
    analysis_id, claim_id = _proposed_claim_on_analysis(client, headers, "Promotion wins")

    promoted, deleted = _race(
        client,
        monkeypatch,
        lambda: _promote(client, headers, claim_id),
        lambda: client.delete(f"/analyses/{analysis_id}", headers=headers),
    )

    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["data"]["status"] == "supported"
    assert deleted.status_code == 422, deleted.text
    assert deleted.json()["error"]["message"] == (
        "Analysis cannot be deleted while it is the last support link for a non-proposed claim."
    )
    with client.app.state.db_session_factory() as session:
        assert session.get(AnalysisModel, analysis_id) is not None
        claim = session.get(ClaimModel, claim_id)
        assert claim is not None
        assert claim.status == "supported"
        assert session.scalar(select(func.count()).select_from(ClaimAnalysisModel)) == 1
