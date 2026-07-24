"""Real PostgreSQL serialization coverage for dataset snapshot updates."""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Lock
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from lab_tracker.application.file_commands import DatasetFileCommands
from lab_tracker.models import (
    AcceptanceMode,
    Dataset,
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
)
from lab_tracker.services.shared import compute_commit_hash
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

pytestmark = pytest.mark.postgres


def _future_result(future: Future[Any]) -> Any:
    return future.result(timeout=20)


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
        f"Backend {blocked_pid} was not blocked by {expected_blocker_pid} before the deadline."
    )


def _create_staged_dataset(
    client: TestClient,
    headers: dict[str, str],
    *,
    label: str,
) -> tuple[UUID, UUID]:
    slug = label.casefold().replace(" ", "-")
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
    dataset_response = client.post(
        "/datasets",
        json={
            "project_id": str(project_id),
            "primary_question_id": question_response.json()["data"]["question_id"],
            "commit_manifest": {
                "external_artifacts": [
                    {
                        "source_system": "s3",
                        "uri": f"s3://lab-tracker/{slug}/manifest.json",
                        "content_hash": f"sha256:{slug}",
                        "metadata": {"backend": "postgresql"},
                    }
                ]
            },
        },
        headers=headers,
    )
    assert dataset_response.status_code == 201, dataset_response.text
    return project_id, UUID(dataset_response.json()["data"]["dataset_id"])


def _upload_file(
    client: TestClient,
    headers: dict[str, str],
    *,
    dataset_id: UUID,
    filename: str,
) -> UUID:
    response = client.post(
        f"/datasets/{dataset_id}/files",
        files={
            "file": (
                filename,
                f"{filename}-content".encode(),
                "application/octet-stream",
            )
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["data"]["file_id"])


def _seed_graph_updates(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: UUID,
    updates: list[tuple[EntityType, UUID, dict[str, object]]],
    label: str,
) -> UUID:
    note_response = client.post(
        "/notes",
        json={
            "project_id": str(project_id),
            "raw_content": f"{label} graph draft source",
        },
        headers=headers,
    )
    assert note_response.status_code == 201, note_response.text
    note_id = UUID(note_response.json()["data"]["note_id"])
    change_set_id = uuid4()
    operations = [
        GraphChangeOperation(
            operation_id=uuid4(),
            change_set_id=change_set_id,
            sequence=sequence,
            op=GraphChangeOp.UPDATE,
            entity_type=entity_type,
            target_entity_id=entity_id,
            payload=payload,
            status=GraphChangeOperationStatus.ACCEPTED,
            acceptance_mode=AcceptanceMode.BULK_ACCEPTED,
        )
        for sequence, (entity_type, entity_id, payload) in enumerate(updates, start=1)
    ]
    change_set = GraphChangeSet(
        change_set_id=change_set_id,
        project_id=project_id,
        source_note_id=note_id,
        source_note_ids=[note_id],
        model="test-model",
        prompt_version="test-prompt",
        status=GraphChangeSetStatus.READY,
        operations=operations,
    )
    with client.app.state.db_session_factory() as session:
        repository = SQLAlchemyLabTrackerRepository(session)
        repository.graph_change_sets.save(change_set)
        repository.commit()
    return change_set_id


def _seed_graph_dataset_updates(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: UUID,
    updates: list[tuple[UUID, dict[str, object]]],
    label: str,
) -> UUID:
    return _seed_graph_updates(
        client,
        headers,
        project_id=project_id,
        updates=[(EntityType.DATASET, dataset_id, payload) for dataset_id, payload in updates],
        label=label,
    )


def _dataset_payload(
    client: TestClient,
    headers: dict[str, str],
    dataset_id: UUID,
) -> dict[str, Any]:
    response = client.get(
        f"/datasets/{dataset_id}",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _manifest_update(
    dataset: dict[str, Any],
    *,
    metadata: dict[str, object],
) -> dict[str, object]:
    manifest = dataset["commit_manifest"]
    return {
        "files": manifest["files"],
        "external_artifacts": manifest["external_artifacts"],
        "metadata": metadata,
        "nwb_metadata": manifest["nwb_metadata"],
        "bids_metadata": manifest["bids_metadata"],
        "note_ids": manifest["note_ids"],
        "source_session_id": manifest["source_session_id"],
    }


@pytest.mark.parametrize("commit_surface", ["provenance", "graph"])
@pytest.mark.parametrize("file_mutation", ["upload", "delete"])
@pytest.mark.parametrize("winner", ["file", "commit"])
def test_file_mutations_and_dataset_commits_are_linearizable(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    commit_surface: str,
    file_mutation: str,
    winner: str,
) -> None:
    project_id, dataset_id = _create_staged_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        label=f"{file_mutation} {commit_surface} {winner}",
    )
    filename = f"{file_mutation}-{commit_surface}-{winner}.bin"
    file_id = (
        _upload_file(
            postgres_client,
            postgres_admin_auth_headers,
            dataset_id=dataset_id,
            filename=filename,
        )
        if file_mutation == "delete"
        else None
    )
    change_set_id = (
        _seed_graph_dataset_updates(
            postgres_client,
            postgres_admin_auth_headers,
            project_id=project_id,
            updates=[(dataset_id, {"status": "committed"})],
            label=f"{file_mutation} {winner}",
        )
        if commit_surface == "graph"
        else None
    )

    release_winner = Event()
    file_mutation_ready = Event()
    file_lock_entered = Event()
    commit_lock_entered = Event()
    commit_lock_acquired = Event()
    backend_pids: dict[str, int] = {}

    original_snapshot_lock = SQLAlchemyLabTrackerRepository.lock_dataset_updates
    snapshot_lock_seen = Event()

    def observed_snapshot_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
        locked_dataset_ids: Iterable[UUID],
    ) -> None:
        ids = tuple(locked_dataset_ids)
        is_target_call = dataset_id in ids and not snapshot_lock_seen.is_set()
        if is_target_call:
            snapshot_lock_seen.set()
            backend_pids["commit"] = _backend_pid(repository)
            commit_lock_entered.set()
        original_snapshot_lock(repository, locked_project_id, ids)
        if is_target_call and winner == "commit":
            commit_lock_acquired.set()
            if not release_winner.wait(timeout=20):
                raise RuntimeError("Timed out holding the dataset snapshot lock.")

    original_file_lock = SQLAlchemyLabTrackerRepository.lock_dataset_file_mutation

    def observed_file_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
        locked_dataset_id: UUID,
    ) -> None:
        if locked_dataset_id == dataset_id:
            backend_pids["file"] = _backend_pid(repository)
            file_lock_entered.set()
        original_file_lock(repository, locked_project_id, locked_dataset_id)

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_dataset_updates",
        observed_snapshot_lock,
    )
    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_dataset_file_mutation",
        observed_file_lock,
    )

    command_name = "upload" if file_mutation == "upload" else "delete"
    original_file_command = getattr(DatasetFileCommands, command_name)

    def observed_file_command(
        commands: DatasetFileCommands,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        result = original_file_command(commands, *args, **kwargs)
        if winner == "file":
            file_mutation_ready.set()
            if not release_winner.wait(timeout=20):
                raise RuntimeError("Timed out holding the dataset-file mutation.")
        return result

    monkeypatch.setattr(
        DatasetFileCommands,
        command_name,
        observed_file_command,
    )

    def mutate_file():
        if file_mutation == "upload":
            return postgres_client.post(
                f"/datasets/{dataset_id}/files",
                files={
                    "file": (
                        filename,
                        b"concurrent-dataset-snapshot",
                        "application/octet-stream",
                    )
                },
                headers=postgres_admin_auth_headers,
            )
        assert file_id is not None
        return postgres_client.delete(
            f"/datasets/{dataset_id}/files/{file_id}",
            headers=postgres_admin_auth_headers,
        )

    def commit_dataset():
        if commit_surface == "provenance":
            return postgres_client.patch(
                f"/datasets/{dataset_id}",
                json={"status": "committed"},
                headers=postgres_admin_auth_headers,
            )
        assert change_set_id is not None
        return postgres_client.post(
            f"/graph-drafts/{change_set_id}/commit",
            json={"message": "Commit the concurrent dataset snapshot"},
            headers=postgres_admin_auth_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        if winner == "file":
            file_future = executor.submit(mutate_file)
            assert file_mutation_ready.wait(timeout=10)
            commit_future = executor.submit(commit_dataset)
            try:
                assert commit_lock_entered.wait(timeout=10)
                assert backend_pids["file"] != backend_pids["commit"]
                _wait_until_blocked(
                    postgres_client,
                    blocked_pid=backend_pids["commit"],
                    expected_blocker_pid=backend_pids["file"],
                )
            finally:
                release_winner.set()
        else:
            commit_future = executor.submit(commit_dataset)
            assert commit_lock_acquired.wait(timeout=10)
            file_future = executor.submit(mutate_file)
            try:
                assert file_lock_entered.wait(timeout=10)
                assert backend_pids["file"] != backend_pids["commit"]
                _wait_until_blocked(
                    postgres_client,
                    blocked_pid=backend_pids["file"],
                    expected_blocker_pid=backend_pids["commit"],
                )
            finally:
                release_winner.set()
        file_response = _future_result(file_future)
        commit_response = _future_result(commit_future)

    assert commit_response.status_code == 200, commit_response.text
    if winner == "file":
        expected_file_status = 201 if file_mutation == "upload" else 200
        assert file_response.status_code == expected_file_status, file_response.text
    else:
        assert file_response.status_code == 422, file_response.text
        assert "dataset status is staged" in file_response.json()["error"]["message"]

    dataset = _dataset_payload(
        postgres_client,
        postgres_admin_auth_headers,
        dataset_id,
    )
    manifest_paths = {item["path"] for item in dataset["commit_manifest"]["files"]}
    expected_file_present = winner == "file" if file_mutation == "upload" else winner == "commit"
    assert (filename in manifest_paths) is expected_file_present
    domain_dataset = Dataset.model_validate(dataset)
    assert dataset["commit_hash"] == compute_commit_hash(domain_dataset.commit_manifest)

    files_response = postgres_client.get(
        f"/datasets/{dataset_id}/files",
        headers=postgres_admin_auth_headers,
    )
    assert files_response.status_code == 200, files_response.text
    attached_paths = {item["path"] for item in files_response.json()["data"]}
    assert (filename in attached_paths) is expected_file_present


@pytest.mark.parametrize("loser_surface", ["direct", "graph"])
@pytest.mark.parametrize("scenario", ["committed_winner", "metadata_winner"])
def test_dataset_snapshot_updates_reload_after_lock_wait(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    loser_surface: str,
    scenario: str,
) -> None:
    project_id, dataset_id = _create_staged_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        label=f"Snapshot reload {scenario} {loser_surface}",
    )
    initial = _dataset_payload(
        postgres_client,
        postgres_admin_auth_headers,
        dataset_id,
    )
    if scenario == "committed_winner":
        winner_payload = {"status": "committed"}
        loser_payload = {
            "commit_manifest": _manifest_update(
                initial,
                metadata={"writer": "stale_manifest_loser"},
            )
        }
    else:
        winner_payload = {
            "commit_manifest": _manifest_update(
                initial,
                metadata={"writer": "metadata_winner"},
            )
        }
        loser_payload = {"status": "committed"}

    loser_change_set_id = (
        _seed_graph_dataset_updates(
            postgres_client,
            postgres_admin_auth_headers,
            project_id=project_id,
            updates=[(dataset_id, loser_payload)],
            label=f"Snapshot loser {scenario}",
        )
        if loser_surface == "graph"
        else None
    )

    original_snapshot_lock = SQLAlchemyLabTrackerRepository.lock_dataset_updates
    call_guard = Lock()
    release_winner = Event()
    winner_locked = Event()
    loser_entered = Event()
    backend_pids: dict[str, int] = {}
    target_lock_calls = 0

    def observed_snapshot_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
        locked_dataset_ids: Iterable[UUID],
    ) -> None:
        nonlocal target_lock_calls
        ids = tuple(locked_dataset_ids)
        if dataset_id not in ids:
            original_snapshot_lock(repository, locked_project_id, ids)
            return
        with call_guard:
            target_lock_calls += 1
            call_number = target_lock_calls
        if call_number == 1:
            backend_pids["winner"] = _backend_pid(repository)
        elif call_number == 2:
            backend_pids["loser"] = _backend_pid(repository)
            loser_entered.set()
        original_snapshot_lock(repository, locked_project_id, ids)
        if call_number == 1:
            winner_locked.set()
            if not release_winner.wait(timeout=20):
                raise RuntimeError("Timed out holding the winning dataset snapshot.")

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_dataset_updates",
        observed_snapshot_lock,
    )

    def update_winner():
        return postgres_client.patch(
            f"/datasets/{dataset_id}",
            json=winner_payload,
            headers=postgres_admin_auth_headers,
        )

    def update_loser():
        if loser_surface == "direct":
            return postgres_client.patch(
                f"/datasets/{dataset_id}",
                json=loser_payload,
                headers=postgres_admin_auth_headers,
            )
        assert loser_change_set_id is not None
        return postgres_client.post(
            f"/graph-drafts/{loser_change_set_id}/commit",
            json={"message": f"Apply losing {scenario} snapshot"},
            headers=postgres_admin_auth_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        winner_future = executor.submit(update_winner)
        assert winner_locked.wait(timeout=10)
        loser_future = executor.submit(update_loser)
        try:
            assert loser_entered.wait(timeout=10)
            assert backend_pids["winner"] != backend_pids["loser"]
            _wait_until_blocked(
                postgres_client,
                blocked_pid=backend_pids["loser"],
                expected_blocker_pid=backend_pids["winner"],
            )
        finally:
            release_winner.set()
        winner_response = _future_result(winner_future)
        loser_response = _future_result(loser_future)

    assert winner_response.status_code == 200, winner_response.text
    final = _dataset_payload(
        postgres_client,
        postgres_admin_auth_headers,
        dataset_id,
    )
    if scenario == "committed_winner":
        assert loser_response.status_code == 422, loser_response.text
        assert "Committed datasets are immutable" in loser_response.json()["error"]["message"]
        assert final == winner_response.json()["data"]
        assert final["status"] == "committed"
        assert final["commit_manifest"]["metadata"] != {"writer": "stale_manifest_loser"}
    else:
        assert loser_response.status_code == 200, loser_response.text
        assert final["status"] == "committed"
        assert final["commit_manifest"]["metadata"] == {"writer": "metadata_winner"}
        assert (
            final["commit_manifest"]["external_artifacts"]
            == initial["commit_manifest"]["external_artifacts"]
        )
    final_dataset = Dataset.model_validate(final)
    assert final["commit_hash"] == compute_commit_hash(final_dataset.commit_manifest)


def test_graph_dataset_update_rollback_releases_lock_for_retry(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, dataset_id = _create_staged_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        label="Graph rollback retry",
    )
    before_failure = _dataset_payload(
        postgres_client,
        postgres_admin_auth_headers,
        dataset_id,
    )
    question_id = UUID(before_failure["primary_question_id"])
    question_before_response = postgres_client.get(
        f"/questions/{question_id}",
        headers=postgres_admin_auth_headers,
    )
    assert question_before_response.status_code == 200, question_before_response.text
    question_before = question_before_response.json()["data"]
    invalid_change_set_id = _seed_graph_updates(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        updates=[
            (
                EntityType.QUESTION,
                question_id,
                {"hypothesis": "This version must roll back."},
            ),
            (EntityType.DATASET, dataset_id, {"status": "committed"}),
            (
                EntityType.DATASET,
                dataset_id,
                {"commit_manifest": {"metadata": {"must": "roll-back"}}},
            ),
        ],
        label="Invalid rollback",
    )
    retry_change_set_id = _seed_graph_dataset_updates(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        updates=[(dataset_id, {"status": "committed"})],
        label="Valid retry",
    )

    original_snapshot_lock = SQLAlchemyLabTrackerRepository.lock_dataset_updates
    call_guard = Lock()
    invalid_locked = Event()
    retry_entered = Event()
    retry_acquired = Event()
    release_invalid = Event()
    release_retry = Event()
    backend_pids: dict[str, int] = {}
    target_lock_calls = 0

    def hold_invalid_then_retry(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
        locked_dataset_ids: Iterable[UUID],
    ) -> None:
        nonlocal target_lock_calls
        ids = tuple(locked_dataset_ids)
        if dataset_id not in ids:
            original_snapshot_lock(repository, locked_project_id, ids)
            return
        with call_guard:
            target_lock_calls += 1
            call_number = target_lock_calls
        if call_number == 1:
            backend_pids["invalid"] = _backend_pid(repository)
        elif call_number == 2:
            backend_pids["retry"] = _backend_pid(repository)
            retry_entered.set()
        original_snapshot_lock(repository, locked_project_id, ids)
        if call_number == 1:
            invalid_locked.set()
            if not release_invalid.wait(timeout=20):
                raise RuntimeError("Timed out holding the invalid graph transaction.")
        elif call_number == 2:
            retry_acquired.set()
            if not release_retry.wait(timeout=20):
                raise RuntimeError("Timed out holding the retry after rollback.")

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_dataset_updates",
        hold_invalid_then_retry,
    )

    def commit(change_set_id: UUID, message: str):
        return postgres_client.post(
            f"/graph-drafts/{change_set_id}/commit",
            json={"message": message},
            headers=postgres_admin_auth_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        invalid_future = executor.submit(
            commit,
            invalid_change_set_id,
            "Must roll back",
        )
        assert invalid_locked.wait(timeout=10)
        retry_future = executor.submit(
            commit,
            retry_change_set_id,
            "Retry after rollback",
        )
        try:
            assert retry_entered.wait(timeout=10)
            assert backend_pids["invalid"] != backend_pids["retry"]
            _wait_until_blocked(
                postgres_client,
                blocked_pid=backend_pids["retry"],
                expected_blocker_pid=backend_pids["invalid"],
            )
            release_invalid.set()
            assert retry_acquired.wait(timeout=10)
            failed_commit = _future_result(invalid_future)

            assert failed_commit.status_code == 422, failed_commit.text
            assert "Committed datasets are immutable" in failed_commit.json()["error"]["message"]
            after_failure = _dataset_payload(
                postgres_client,
                postgres_admin_auth_headers,
                dataset_id,
            )
            assert after_failure == before_failure
            question_after_response = postgres_client.get(
                f"/questions/{question_id}",
                headers=postgres_admin_auth_headers,
            )
            assert question_after_response.status_code == 200, question_after_response.text
            assert question_after_response.json()["data"] == question_before

            failed_draft_response = postgres_client.get(
                f"/graph-drafts/{invalid_change_set_id}",
                headers=postgres_admin_auth_headers,
            )
            assert failed_draft_response.status_code == 200, failed_draft_response.text
            failed_draft = failed_draft_response.json()["data"]
            assert failed_draft["status"] == "ready"
            assert failed_draft["commit_message"] is None
            assert failed_draft["committed_at"] is None
            assert len(failed_draft["operations"]) == 3
            assert all(
                operation["status"] == "accepted" and operation["result_entity_id"] is None
                for operation in failed_draft["operations"]
            )
            with postgres_client.app.state.db_session_factory() as session:
                repository = SQLAlchemyLabTrackerRepository(session)
                versions, version_total = repository.query_entity_versions(
                    change_set_id=invalid_change_set_id,
                    limit=None,
                    offset=0,
                )
            assert versions == []
            assert version_total == 0
        finally:
            release_invalid.set()
            release_retry.set()
        retry = _future_result(retry_future)

    assert retry.status_code == 200, retry.text
    committed = _dataset_payload(
        postgres_client,
        postgres_admin_auth_headers,
        dataset_id,
    )
    assert committed["status"] == "committed"


@pytest.mark.parametrize("winner", ["graph", "delete"])
def test_project_delete_and_graph_commit_have_one_linear_winner(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    winner: str,
) -> None:
    project_id, dataset_id = _create_staged_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        label=f"Project delete versus graph {winner}",
    )
    change_set_id = _seed_graph_dataset_updates(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        updates=[
            (
                dataset_id,
                {"commit_manifest": {"metadata": {"winner": "graph"}}},
            )
        ],
        label=f"Project delete graph {winner}",
    )

    original_guard = SQLAlchemyLabTrackerRepository.lock_project_deletion_guard
    original_claim = SQLAlchemyLabTrackerRepository.claim_graph_change_set_for_commit
    original_delete_lock = SQLAlchemyLabTrackerRepository.lock_project_deletion
    graph_guard_entered = Event()
    graph_claimed = Event()
    delete_lock_entered = Event()
    delete_locked = Event()
    release_winner = Event()
    backend_pids: dict[str, int] = {}

    def observed_graph_guard(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
    ) -> None:
        if locked_project_id == project_id:
            backend_pids["graph"] = _backend_pid(repository)
            graph_guard_entered.set()
        original_guard(repository, locked_project_id)

    def held_graph_claim(
        repository: SQLAlchemyLabTrackerRepository,
        claimed_change_set_id: UUID,
    ):
        claimed = original_claim(repository, claimed_change_set_id)
        if winner == "graph" and claimed_change_set_id == change_set_id:
            graph_claimed.set()
            if not release_winner.wait(timeout=20):
                raise RuntimeError("Timed out holding the claimed graph change set.")
        return claimed

    def held_project_delete(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
    ) -> None:
        if locked_project_id != project_id:
            original_delete_lock(repository, locked_project_id)
            return
        backend_pids["delete"] = _backend_pid(repository)
        delete_lock_entered.set()
        original_delete_lock(repository, locked_project_id)
        if winner == "delete":
            delete_locked.set()
            if not release_winner.wait(timeout=20):
                raise RuntimeError("Timed out holding the project-deletion lock.")

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_project_deletion_guard",
        observed_graph_guard,
    )
    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "claim_graph_change_set_for_commit",
        held_graph_claim,
    )
    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_project_deletion",
        held_project_delete,
    )

    def commit_graph():
        return postgres_client.post(
            f"/graph-drafts/{change_set_id}/commit",
            json={"message": "Race graph commit with project deletion"},
            headers=postgres_admin_auth_headers,
        )

    def delete_project():
        return postgres_client.delete(
            f"/projects/{project_id}",
            headers=postgres_admin_auth_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        if winner == "graph":
            graph_future = executor.submit(commit_graph)
            assert graph_claimed.wait(timeout=10)
            delete_future = executor.submit(delete_project)
            try:
                assert delete_lock_entered.wait(timeout=10)
                assert backend_pids["graph"] != backend_pids["delete"]
                _wait_until_blocked(
                    postgres_client,
                    blocked_pid=backend_pids["delete"],
                    expected_blocker_pid=backend_pids["graph"],
                )
            finally:
                release_winner.set()
        else:
            delete_future = executor.submit(delete_project)
            assert delete_locked.wait(timeout=10)
            graph_future = executor.submit(commit_graph)
            try:
                assert graph_guard_entered.wait(timeout=10)
                assert backend_pids["graph"] != backend_pids["delete"]
                _wait_until_blocked(
                    postgres_client,
                    blocked_pid=backend_pids["graph"],
                    expected_blocker_pid=backend_pids["delete"],
                )
            finally:
                release_winner.set()
        graph_response = _future_result(graph_future)
        delete_response = _future_result(delete_future)

    assert delete_response.status_code == 200, delete_response.text
    if winner == "graph":
        assert graph_response.status_code == 200, graph_response.text
        assert graph_response.json()["data"]["status"] == "committed"
    else:
        assert graph_response.status_code == 404, graph_response.text
    assert graph_response.status_code < 500
    assert (
        postgres_client.get(
            f"/projects/{project_id}",
            headers=postgres_admin_auth_headers,
        ).status_code
        == 404
    )
    assert (
        postgres_client.get(
            f"/datasets/{dataset_id}",
            headers=postgres_admin_auth_headers,
        ).status_code
        == 404
    )
    assert (
        postgres_client.get(
            f"/graph-drafts/{change_set_id}",
            headers=postgres_admin_auth_headers,
        ).status_code
        == 404
    )


def test_reverse_multi_dataset_graph_updates_prelock_without_deadlock(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, first_dataset_id = _create_staged_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        label="Reverse graph first",
    )
    question_response = postgres_client.post(
        "/questions",
        json={
            "project_id": str(project_id),
            "text": "Reverse graph second question",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=postgres_admin_auth_headers,
    )
    assert question_response.status_code == 201, question_response.text
    second_dataset_response = postgres_client.post(
        "/datasets",
        json={
            "project_id": str(project_id),
            "primary_question_id": question_response.json()["data"]["question_id"],
        },
        headers=postgres_admin_auth_headers,
    )
    assert second_dataset_response.status_code == 201, second_dataset_response.text
    second_dataset_id = UUID(second_dataset_response.json()["data"]["dataset_id"])
    first_change_set_id = _seed_graph_dataset_updates(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        updates=[
            (
                first_dataset_id,
                {"commit_manifest": {"metadata": {"batch": "first", "dataset": "one"}}},
            ),
            (
                second_dataset_id,
                {"commit_manifest": {"metadata": {"batch": "first", "dataset": "two"}}},
            ),
        ],
        label="First reverse order",
    )
    second_change_set_id = _seed_graph_dataset_updates(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        updates=[
            (
                second_dataset_id,
                {"commit_manifest": {"metadata": {"batch": "second", "dataset": "two"}}},
            ),
            (
                first_dataset_id,
                {"commit_manifest": {"metadata": {"batch": "second", "dataset": "one"}}},
            ),
        ],
        label="Second reverse order",
    )

    original_snapshot_lock = SQLAlchemyLabTrackerRepository.lock_dataset_updates
    guard = Lock()
    first_locked = Event()
    release_first = Event()
    second_entered = Event()
    backend_pids: dict[str, int] = {}
    lock_plans: list[list[UUID]] = []
    multi_lock_calls = 0

    def hold_first_multi_dataset_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
        locked_dataset_ids: Iterable[UUID],
    ) -> None:
        nonlocal multi_lock_calls
        ids = list(locked_dataset_ids)
        if set(ids) != {first_dataset_id, second_dataset_id}:
            original_snapshot_lock(repository, locked_project_id, ids)
            return
        with guard:
            multi_lock_calls += 1
            call_number = multi_lock_calls
            lock_plans.append(ids)
        backend_pids[f"commit_{call_number}"] = _backend_pid(repository)
        if call_number == 2:
            second_entered.set()
        original_snapshot_lock(repository, locked_project_id, ids)
        if call_number == 1:
            first_locked.set()
            if not release_first.wait(timeout=20):
                raise RuntimeError("Timed out holding the first multi-dataset lock.")

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_dataset_updates",
        hold_first_multi_dataset_lock,
    )

    def commit(change_set_id: UUID, message: str):
        return postgres_client.post(
            f"/graph-drafts/{change_set_id}/commit",
            json={"message": message},
            headers=postgres_admin_auth_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            commit,
            first_change_set_id,
            "First canonical lock plan",
        )
        assert first_locked.wait(timeout=10)
        second_future = executor.submit(
            commit,
            second_change_set_id,
            "Second reverse operation order",
        )
        try:
            assert second_entered.wait(timeout=10)
            assert backend_pids["commit_1"] != backend_pids["commit_2"]
            _wait_until_blocked(
                postgres_client,
                blocked_pid=backend_pids["commit_2"],
                expected_blocker_pid=backend_pids["commit_1"],
            )
        finally:
            release_first.set()
        first_response = _future_result(first_future)
        second_response = _future_result(second_future)

    assert first_response.status_code == 200, first_response.text
    assert second_response.status_code == 200, second_response.text
    canonical_order = sorted([first_dataset_id, second_dataset_id], key=str)
    assert lock_plans == [canonical_order, canonical_order]
    first_dataset = _dataset_payload(
        postgres_client,
        postgres_admin_auth_headers,
        first_dataset_id,
    )
    second_dataset = _dataset_payload(
        postgres_client,
        postgres_admin_auth_headers,
        second_dataset_id,
    )
    assert first_dataset["commit_manifest"]["metadata"] == {
        "batch": "second",
        "dataset": "one",
    }
    assert second_dataset["commit_manifest"]["metadata"] == {
        "batch": "second",
        "dataset": "two",
    }
    assert first_dataset["change_set_id"] == str(second_change_set_id)
    assert second_dataset["change_set_id"] == str(second_change_set_id)
