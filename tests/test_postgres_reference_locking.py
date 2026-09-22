"""PostgreSQL races between reference guards and reference-adding writes.

Each test holds the first transaction inside ``lock_project_references`` until
the second request is provably blocked on the same advisory lock, then lets
the first commit and checks the loser observed the winner (M56, M52).
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

from lab_tracker.db_models import (
    ClaimDatasetModel,
    ClaimEdgeModel,
    ClaimModel,
    DatasetModel,
    GoalLinkModel,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

pytestmark = pytest.mark.postgres


def _post(client: TestClient, headers: dict[str, str], path: str, payload: dict[str, Any]):
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _project_with_dataset(
    client: TestClient,
    headers: dict[str, str],
    name: str,
) -> tuple[UUID, UUID]:
    project = _post(client, headers, "/projects", {"name": name})
    question = _post(
        client,
        headers,
        "/questions",
        {
            "project_id": project["project_id"],
            "text": "Which dataset survives?",
            "question_type": "descriptive",
            "status": "active",
        },
    )
    dataset = _post(
        client,
        headers,
        "/datasets",
        {
            "project_id": project["project_id"],
            "primary_question_id": question["question_id"],
        },
    )
    return UUID(project["project_id"]), UUID(dataset["dataset_id"])


def _create_claim(
    client: TestClient,
    headers: dict[str, str],
    project_id: UUID,
    *,
    statement: str,
    dataset_id: UUID | None = None,
):
    payload: dict[str, Any] = {
        "project_id": str(project_id),
        "statement": statement,
        "confidence": 0.5,
    }
    if dataset_id is not None:
        payload["supported_by_dataset_ids"] = [str(dataset_id)]
    return client.post("/claims", json=payload, headers=headers)


def _blocking_pids(client: TestClient, blocked_pid: int) -> list[int]:
    with client.app.state.db_engine.connect() as connection:
        result = connection.scalar(
            text("SELECT pg_blocking_pids(:blocked_pid)"),
            {"blocked_pid": blocked_pid},
        )
    return [int(value) for value in result or []]


def _wait_until_blocked(client: TestClient, *, blocked_pid: int, blocker_pid: int) -> None:
    deadline = monotonic() + 10
    poll = Event()
    while monotonic() < deadline:
        if blocker_pid in _blocking_pids(client, blocked_pid):
            return
        poll.wait(timeout=0.01)
    pytest.fail(f"Backend {blocked_pid} was not blocked by {blocker_pid} before the deadline.")


def _backend_pid(repository: SQLAlchemyLabTrackerRepository) -> int:
    value = repository._session.scalar(text("SELECT pg_backend_pid()"))  # noqa: SLF001
    assert value is not None
    return int(value)


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


def test_dataset_delete_first_makes_concurrent_claim_create_fail_cleanly(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = postgres_admin_auth_headers
    project_id, dataset_id = _project_with_dataset(postgres_client, headers, "Delete wins")

    deleted, created = _race(
        postgres_client,
        monkeypatch,
        lambda: postgres_client.delete(f"/datasets/{dataset_id}", headers=headers),
        lambda: _create_claim(
            postgres_client,
            headers,
            project_id,
            statement="Supported by a dataset being deleted.",
            dataset_id=dataset_id,
        ),
    )

    assert deleted.status_code == 200, deleted.text
    assert created.status_code == 404, created.text
    assert created.json()["error"]["code"] == "not_found"
    with postgres_client.app.state.db_session_factory() as session:
        assert session.get(DatasetModel, str(dataset_id)) is None
        assert session.scalar(select(func.count()).select_from(ClaimModel)) == 0
        assert session.scalar(select(func.count()).select_from(ClaimDatasetModel)) == 0


def test_claim_create_first_blocks_concurrent_dataset_delete(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = postgres_admin_auth_headers
    project_id, dataset_id = _project_with_dataset(postgres_client, headers, "Claim wins")

    created, deleted = _race(
        postgres_client,
        monkeypatch,
        lambda: _create_claim(
            postgres_client,
            headers,
            project_id,
            statement="Supported before the delete.",
            dataset_id=dataset_id,
        ),
        lambda: postgres_client.delete(f"/datasets/{dataset_id}", headers=headers),
    )

    assert created.status_code == 201, created.text
    assert deleted.status_code == 422, deleted.text
    assert deleted.json()["error"]["message"] == (
        "Dataset cannot be deleted while claims reference it."
    )
    with postgres_client.app.state.db_session_factory() as session:
        assert session.get(DatasetModel, str(dataset_id)) is not None
        assert session.scalar(select(func.count()).select_from(ClaimDatasetModel)) == 1


def test_opposite_claim_edges_serialize_and_cannot_commit_a_cycle(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = postgres_admin_auth_headers
    project = _post(postgres_client, headers, "/projects", {"name": "Claim edge race"})
    project_id = UUID(project["project_id"])
    claims = []
    for statement in ("Claim A.", "Claim B."):
        response = _create_claim(postgres_client, headers, project_id, statement=statement)
        assert response.status_code == 201, response.text
        claims.append(response.json()["data"]["claim_id"])
    claim_a, claim_b = claims

    def create_edge(source: str, target: str):
        return postgres_client.post(
            f"/claims/{source}/edges",
            json={"target_claim_id": target, "relation": "depends_on"},
            headers=headers,
        )

    first, second = _race(
        postgres_client,
        monkeypatch,
        lambda: create_edge(claim_a, claim_b),
        lambda: create_edge(claim_b, claim_a),
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 422, second.text
    assert second.json()["error"]["message"] == "Claim edge would create a cycle."
    with postgres_client.app.state.db_session_factory() as session:
        edges = list(
            session.execute(select(ClaimEdgeModel.claim_id, ClaimEdgeModel.target_claim_id))
        )
    assert [(str(source), str(target)) for source, target in edges] == [(claim_a, claim_b)]


def _project_goal_and_question(
    client: TestClient,
    headers: dict[str, str],
    name: str,
) -> tuple[str, str]:
    project = _post(client, headers, "/projects", {"name": name})
    question = _post(
        client,
        headers,
        "/questions",
        {
            "project_id": project["project_id"],
            "text": "Which goal link survives?",
            "question_type": "descriptive",
        },
    )
    goal = _post(
        client,
        headers,
        f"/projects/{project['project_id']}/goals",
        {"goal_type": "paper", "title": "Paper", "summary": "Original summary."},
    )
    return goal["goal_id"], question["question_id"]


def _link_goal(client: TestClient, headers: dict[str, str], goal_id: str, question_id: str):
    return client.post(
        f"/goals/{goal_id}/links",
        json={"entity_type": "question", "entity_id": question_id, "relation": "addresses"},
        headers=headers,
    )


def _goal_link_targets(client: TestClient) -> list[str]:
    with client.app.state.db_session_factory() as session:
        return [str(value) for value in session.scalars(select(GoalLinkModel.entity_id))]


def test_question_delete_first_makes_concurrent_goal_link_fail_cleanly(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = postgres_admin_auth_headers
    goal_id, question_id = _project_goal_and_question(
        postgres_client, headers, "Delete beats goal link"
    )

    deleted, linked = _race(
        postgres_client,
        monkeypatch,
        lambda: postgres_client.delete(f"/questions/{question_id}", headers=headers),
        lambda: _link_goal(postgres_client, headers, goal_id, question_id),
    )

    assert deleted.status_code == 200, deleted.text
    assert linked.status_code == 404, linked.text
    assert linked.json()["error"]["message"] == "Question does not exist."
    assert _goal_link_targets(postgres_client) == []


def test_goal_link_first_is_cleaned_up_by_concurrent_question_delete(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = postgres_admin_auth_headers
    goal_id, question_id = _project_goal_and_question(
        postgres_client, headers, "Goal link beats delete"
    )

    linked, deleted = _race(
        postgres_client,
        monkeypatch,
        lambda: _link_goal(postgres_client, headers, goal_id, question_id),
        lambda: postgres_client.delete(f"/questions/{question_id}", headers=headers),
    )

    assert linked.status_code == 201, linked.text
    assert deleted.status_code == 200, deleted.text
    assert _goal_link_targets(postgres_client) == []
    listed = postgres_client.get("/goals", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [goal["goal_id"] for goal in listed.json()["data"]] == [goal_id]


def _patch_goal(client: TestClient, headers: dict[str, str], goal_id: str, payload: dict):
    return client.patch(f"/goals/{goal_id}", json=payload, headers=headers)


def test_goal_patch_waits_for_a_concurrent_link_and_keeps_it(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = postgres_admin_auth_headers
    goal_id, question_id = _project_goal_and_question(
        postgres_client, headers, "Goal link then patch"
    )

    linked, patched = _race(
        postgres_client,
        monkeypatch,
        lambda: _link_goal(postgres_client, headers, goal_id, question_id),
        lambda: _patch_goal(
            postgres_client, headers, goal_id, {"summary": "Edited during the link."}
        ),
    )

    assert linked.status_code == 201, linked.text
    assert patched.status_code == 200, patched.text
    fetched = postgres_client.get(f"/goals/{goal_id}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    goal = fetched.json()["data"]
    assert goal["summary"] == "Edited during the link."
    assert [link["link_id"] for link in goal["links"]] == [linked.json()["data"]["link_id"]]


def test_concurrent_goal_patches_re_read_the_goal_under_the_lock(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = postgres_admin_auth_headers
    goal_id, _question_id = _project_goal_and_question(
        postgres_client, headers, "Goal patches serialize"
    )

    summary_patch, title_patch = _race(
        postgres_client,
        monkeypatch,
        lambda: _patch_goal(postgres_client, headers, goal_id, {"summary": "From the winner."}),
        lambda: _patch_goal(postgres_client, headers, goal_id, {"title": "From the loser"}),
    )

    assert summary_patch.status_code == 200, summary_patch.text
    assert title_patch.status_code == 200, title_patch.text
    fetched = postgres_client.get(f"/goals/{goal_id}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["data"]["title"] == "From the loser"
    assert fetched.json()["data"]["summary"] == "From the winner."


def _projectless_goal_reaching(
    client: TestClient,
    headers: dict[str, str],
) -> tuple[str, str, str]:
    """A projectless goal linked to ``Home`` plus a question in ``Doomed``."""

    home = _post(client, headers, "/projects", {"name": "Home"})
    doomed = _post(client, headers, "/projects", {"name": "Doomed"})
    question = _post(
        client,
        headers,
        "/questions",
        {
            "project_id": doomed["project_id"],
            "text": "Does the link outlive the project?",
            "question_type": "descriptive",
        },
    )
    goal = _post(
        client,
        headers,
        "/goals",
        {
            "goal_type": "paper",
            "title": "Cross-project paper",
            "links": [
                {
                    "entity_type": "project",
                    "entity_id": home["project_id"],
                    "relation": "contributes_to",
                }
            ],
        },
    )
    return goal["goal_id"], doomed["project_id"], question["question_id"]


def _race_goal_link_with_project_delete(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    link: Callable[[], Any],
    delete: Callable[[], Any],
    link_first: bool,
) -> tuple[Any, Any]:
    """Pause the first writer after its locks until the second is provably blocked.

    The goal writer pauses in ``GoalService._save_goal`` (after its locked
    validation); the project delete pauses right after
    ``lock_project_deletion``.
    """

    from lab_tracker.services.goal_service import GoalService

    first_paused = Event()
    release_first = Event()
    backend_pids: dict[str, int] = {}
    original_save = GoalService._save_goal
    original_lock_deletion = SQLAlchemyLabTrackerRepository.lock_project_deletion

    def paused_save(self: GoalService, goal: Any) -> None:
        backend_pids["link"] = _backend_pid(self.repository)  # type: ignore[arg-type]
        if link_first:
            first_paused.set()
            if not release_first.wait(timeout=20):
                raise RuntimeError("Timed out holding the goal writer's locks.")
        original_save(self, goal)

    def paused_lock_deletion(
        repository: SQLAlchemyLabTrackerRepository, project_id: UUID
    ) -> None:
        backend_pids["delete"] = _backend_pid(repository)
        original_lock_deletion(repository, project_id)
        if not link_first:
            first_paused.set()
            if not release_first.wait(timeout=20):
                raise RuntimeError("Timed out holding the project deletion lock.")

    def record_link_pid(repository: SQLAlchemyLabTrackerRepository, project_id: UUID) -> None:
        backend_pids.setdefault("link", _backend_pid(repository))
        original_lock_references(repository, project_id)

    original_lock_references = SQLAlchemyLabTrackerRepository.lock_project_references
    monkeypatch.setattr(GoalService, "_save_goal", paused_save)
    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository, "lock_project_deletion", paused_lock_deletion
    )
    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository, "lock_project_references", record_link_pid
    )
    first, second = (link, delete) if link_first else (delete, link)
    blocked, blocker = ("delete", "link") if link_first else ("link", "delete")
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future: Future[Any] = executor.submit(first)
        assert first_paused.wait(timeout=10)
        second_future: Future[Any] = executor.submit(second)
        try:
            deadline = monotonic() + 10
            while blocked not in backend_pids and monotonic() < deadline:
                Event().wait(timeout=0.01)
            assert blocked in backend_pids
            _wait_until_blocked(
                client,
                blocked_pid=backend_pids[blocked],
                blocker_pid=backend_pids[blocker],
            )
        finally:
            release_first.set()
        first_result = first_future.result(timeout=20)
        second_result = second_future.result(timeout=20)
    return (first_result, second_result) if link_first else (second_result, first_result)


def test_projectless_goal_link_first_is_cleaned_up_by_concurrent_project_delete(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = postgres_admin_auth_headers
    goal_id, doomed_id, question_id = _projectless_goal_reaching(postgres_client, headers)

    linked, deleted = _race_goal_link_with_project_delete(
        postgres_client,
        monkeypatch,
        link=lambda: _link_goal(postgres_client, headers, goal_id, question_id),
        delete=lambda: postgres_client.delete(f"/projects/{doomed_id}", headers=headers),
        link_first=True,
    )

    assert linked.status_code == 201, linked.text
    assert deleted.status_code == 200, deleted.text
    assert question_id not in _goal_link_targets(postgres_client)
    fetched = postgres_client.get(f"/goals/{goal_id}", headers=headers)
    assert fetched.status_code == 200, fetched.text


def test_project_delete_first_makes_concurrent_projectless_goal_link_fail_cleanly(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = postgres_admin_auth_headers
    goal_id, doomed_id, question_id = _projectless_goal_reaching(postgres_client, headers)

    linked, deleted = _race_goal_link_with_project_delete(
        postgres_client,
        monkeypatch,
        link=lambda: _link_goal(postgres_client, headers, goal_id, question_id),
        delete=lambda: postgres_client.delete(f"/projects/{doomed_id}", headers=headers),
        link_first=False,
    )

    assert deleted.status_code == 200, deleted.text
    assert linked.status_code == 404, linked.text
    assert question_id not in _goal_link_targets(postgres_client)
    fetched = postgres_client.get(f"/goals/{goal_id}", headers=headers)
    assert fetched.status_code == 200, fetched.text
