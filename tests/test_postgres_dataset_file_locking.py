"""Real PostgreSQL serialization coverage for dataset-file lifecycle races."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from lab_tracker.application.file_commands import DatasetFileCommands
from lab_tracker.db_models import DatasetFileModel
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

pytestmark = pytest.mark.postgres


def _count_suffix(root: Path, suffix: str) -> int:
    if not root.exists():
        return 0
    return sum(1 for _ in root.rglob(f"*{suffix}"))


def _create_dataset(
    client: TestClient,
    headers: dict[str, str],
    *,
    label: str,
) -> tuple[UUID, UUID]:
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
        },
        headers=headers,
    )
    assert question_response.status_code == 201, question_response.text
    dataset_response = client.post(
        "/datasets",
        json={
            "project_id": str(project_id),
            "primary_question_id": question_response.json()["data"]["question_id"],
        },
        headers=headers,
    )
    assert dataset_response.status_code == 201, dataset_response.text
    return project_id, UUID(dataset_response.json()["data"]["dataset_id"])


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


def _future_result(future: Future[Any]) -> Any:
    return future.result(timeout=20)


def _assert_no_dataset_files(
    client: TestClient,
    *,
    storage_root: Path,
) -> None:
    with client.app.state.db_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DatasetFileModel)) == 0
    assert _count_suffix(storage_root, ".bin") == 0
    assert _count_suffix(storage_root, ".json") == 0


def test_upload_then_dataset_delete_waits_and_cleans_the_winning_blob(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, dataset_id = _create_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        label="Upload versus dataset delete",
    )
    storage = postgres_client.app.state.file_storage_backend
    storage_root = Path(storage.base_path)
    blob_stored = Event()
    release_upload = Event()
    delete_entered_lock = Event()
    backend_pids: dict[str, int] = {}

    original_store_stream = storage.store_stream

    def paused_store_stream(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        metadata = original_store_stream(*args, **kwargs)
        blob_stored.set()
        if not release_upload.wait(timeout=20):
            raise RuntimeError("Timed out holding the upload after storing its blob.")
        return metadata

    monkeypatch.setattr(storage, "store_stream", paused_store_stream)

    original_file_lock = SQLAlchemyLabTrackerRepository.lock_dataset_file_mutation
    original_delete_lock = SQLAlchemyLabTrackerRepository.lock_dataset_deletion

    def observed_file_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
        locked_dataset_id: UUID,
    ) -> None:
        backend_pids["upload"] = _backend_pid(repository)
        original_file_lock(repository, locked_project_id, locked_dataset_id)

    def observed_delete_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
        locked_dataset_id: UUID,
    ) -> None:
        backend_pids["delete"] = _backend_pid(repository)
        delete_entered_lock.set()
        original_delete_lock(repository, locked_project_id, locked_dataset_id)

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_dataset_file_mutation",
        observed_file_lock,
    )
    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_dataset_deletion",
        observed_delete_lock,
    )

    def upload():
        return postgres_client.post(
            f"/datasets/{dataset_id}/files",
            files={"file": ("race.bin", b"upload-delete-race", "application/octet-stream")},
            headers=postgres_admin_auth_headers,
        )

    def delete():
        return postgres_client.delete(
            f"/datasets/{dataset_id}",
            headers=postgres_admin_auth_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        upload_future = executor.submit(upload)
        assert blob_stored.wait(timeout=10)
        delete_future = executor.submit(delete)
        try:
            assert delete_entered_lock.wait(timeout=10)
            assert backend_pids["upload"] != backend_pids["delete"]
            _wait_until_blocked(
                postgres_client,
                blocked_pid=backend_pids["delete"],
                expected_blocker_pid=backend_pids["upload"],
            )
        finally:
            release_upload.set()
        upload_response = _future_result(upload_future)
        delete_response = _future_result(delete_future)

    assert upload_response.status_code == 201, upload_response.text
    assert delete_response.status_code == 200, delete_response.text
    assert (
        postgres_client.get(
            f"/datasets/{dataset_id}",
            headers=postgres_admin_auth_headers,
        ).status_code
        == 404
    )
    _assert_no_dataset_files(postgres_client, storage_root=storage_root)
    assert project_id


def test_upload_then_project_delete_waits_and_cleans_the_winning_blob(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, dataset_id = _create_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        label="Upload versus project delete",
    )
    storage = postgres_client.app.state.file_storage_backend
    storage_root = Path(storage.base_path)
    blob_stored = Event()
    release_upload = Event()
    delete_entered_lock = Event()
    backend_pids: dict[str, int] = {}

    original_store_stream = storage.store_stream

    def paused_store_stream(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        metadata = original_store_stream(*args, **kwargs)
        blob_stored.set()
        if not release_upload.wait(timeout=20):
            raise RuntimeError("Timed out holding the upload after storing its blob.")
        return metadata

    monkeypatch.setattr(storage, "store_stream", paused_store_stream)

    original_file_lock = SQLAlchemyLabTrackerRepository.lock_dataset_file_mutation
    original_project_lock = SQLAlchemyLabTrackerRepository.lock_project_deletion

    def observed_file_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
        locked_dataset_id: UUID,
    ) -> None:
        backend_pids["upload"] = _backend_pid(repository)
        original_file_lock(repository, locked_project_id, locked_dataset_id)

    def observed_project_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
    ) -> None:
        backend_pids["delete"] = _backend_pid(repository)
        delete_entered_lock.set()
        original_project_lock(repository, locked_project_id)

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_dataset_file_mutation",
        observed_file_lock,
    )
    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_project_deletion",
        observed_project_lock,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        upload_future = executor.submit(
            postgres_client.post,
            f"/datasets/{dataset_id}/files",
            files={
                "file": (
                    "project-race.bin",
                    b"upload-project-delete-race",
                    "application/octet-stream",
                )
            },
            headers=postgres_admin_auth_headers,
        )
        assert blob_stored.wait(timeout=10)
        delete_future = executor.submit(
            postgres_client.delete,
            f"/projects/{project_id}",
            headers=postgres_admin_auth_headers,
        )
        try:
            assert delete_entered_lock.wait(timeout=10)
            assert backend_pids["upload"] != backend_pids["delete"]
            _wait_until_blocked(
                postgres_client,
                blocked_pid=backend_pids["delete"],
                expected_blocker_pid=backend_pids["upload"],
            )
        finally:
            release_upload.set()
        upload_response = _future_result(upload_future)
        delete_response = _future_result(delete_future)

    assert upload_response.status_code == 201, upload_response.text
    assert delete_response.status_code == 200, delete_response.text
    assert (
        postgres_client.get(
            f"/projects/{project_id}",
            headers=postgres_admin_auth_headers,
        ).status_code
        == 404
    )
    _assert_no_dataset_files(postgres_client, storage_root=storage_root)


def test_dataset_delete_then_upload_rechecks_after_wait_without_storing(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, dataset_id = _create_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        label="Dataset delete wins",
    )
    storage = postgres_client.app.state.file_storage_backend
    storage_root = Path(storage.base_path)
    delete_locked = Event()
    release_delete = Event()
    upload_entered_lock = Event()
    store_called = Event()
    backend_pids: dict[str, int] = {}

    original_delete_lock = SQLAlchemyLabTrackerRepository.lock_dataset_deletion
    original_file_lock = SQLAlchemyLabTrackerRepository.lock_dataset_file_mutation
    original_store_stream = storage.store_stream

    def held_delete_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
        locked_dataset_id: UUID,
    ) -> None:
        backend_pids["delete"] = _backend_pid(repository)
        original_delete_lock(repository, locked_project_id, locked_dataset_id)
        delete_locked.set()
        if not release_delete.wait(timeout=20):
            raise RuntimeError("Timed out holding the dataset-deletion lock.")

    def observed_file_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
        locked_dataset_id: UUID,
    ) -> None:
        backend_pids["upload"] = _backend_pid(repository)
        upload_entered_lock.set()
        original_file_lock(repository, locked_project_id, locked_dataset_id)

    def observed_store_stream(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        store_called.set()
        return original_store_stream(*args, **kwargs)

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_dataset_deletion",
        held_delete_lock,
    )
    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_dataset_file_mutation",
        observed_file_lock,
    )
    monkeypatch.setattr(storage, "store_stream", observed_store_stream)

    with ThreadPoolExecutor(max_workers=2) as executor:
        delete_future = executor.submit(
            postgres_client.delete,
            f"/datasets/{dataset_id}",
            headers=postgres_admin_auth_headers,
        )
        assert delete_locked.wait(timeout=10)
        upload_future = executor.submit(
            postgres_client.post,
            f"/datasets/{dataset_id}/files",
            files={
                "file": (
                    "must-not-store.bin",
                    b"losing-upload",
                    "application/octet-stream",
                )
            },
            headers=postgres_admin_auth_headers,
        )
        try:
            assert upload_entered_lock.wait(timeout=10)
            assert backend_pids["delete"] != backend_pids["upload"]
            _wait_until_blocked(
                postgres_client,
                blocked_pid=backend_pids["upload"],
                expected_blocker_pid=backend_pids["delete"],
            )
        finally:
            release_delete.set()
        delete_response = _future_result(delete_future)
        upload_response = _future_result(upload_future)

    assert delete_response.status_code == 200, delete_response.text
    assert upload_response.status_code == 404, upload_response.text
    assert not store_called.is_set()
    _assert_no_dataset_files(postgres_client, storage_root=storage_root)
    assert project_id


def test_project_delete_then_upload_rechecks_after_wait_without_storing(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, dataset_id = _create_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        label="Project delete wins",
    )
    storage = postgres_client.app.state.file_storage_backend
    storage_root = Path(storage.base_path)
    delete_locked = Event()
    release_delete = Event()
    upload_entered_lock = Event()
    store_called = Event()
    backend_pids: dict[str, int] = {}

    original_project_lock = SQLAlchemyLabTrackerRepository.lock_project_deletion
    original_file_lock = SQLAlchemyLabTrackerRepository.lock_dataset_file_mutation
    original_store_stream = storage.store_stream

    def held_project_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
    ) -> None:
        backend_pids["delete"] = _backend_pid(repository)
        original_project_lock(repository, locked_project_id)
        delete_locked.set()
        if not release_delete.wait(timeout=20):
            raise RuntimeError("Timed out holding the project-deletion lock.")

    def observed_file_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
        locked_dataset_id: UUID,
    ) -> None:
        backend_pids["upload"] = _backend_pid(repository)
        upload_entered_lock.set()
        original_file_lock(repository, locked_project_id, locked_dataset_id)

    def observed_store_stream(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        store_called.set()
        return original_store_stream(*args, **kwargs)

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_project_deletion",
        held_project_lock,
    )
    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_dataset_file_mutation",
        observed_file_lock,
    )
    monkeypatch.setattr(storage, "store_stream", observed_store_stream)

    with ThreadPoolExecutor(max_workers=2) as executor:
        delete_future = executor.submit(
            postgres_client.delete,
            f"/projects/{project_id}",
            headers=postgres_admin_auth_headers,
        )
        assert delete_locked.wait(timeout=10)
        upload_future = executor.submit(
            postgres_client.post,
            f"/datasets/{dataset_id}/files",
            files={
                "file": (
                    "must-not-store-after-project-delete.bin",
                    b"losing-project-upload",
                    "application/octet-stream",
                )
            },
            headers=postgres_admin_auth_headers,
        )
        try:
            assert upload_entered_lock.wait(timeout=10)
            assert backend_pids["delete"] != backend_pids["upload"]
            _wait_until_blocked(
                postgres_client,
                blocked_pid=backend_pids["upload"],
                expected_blocker_pid=backend_pids["delete"],
            )
        finally:
            release_delete.set()
        delete_response = _future_result(delete_future)
        upload_response = _future_result(upload_future)

    assert delete_response.status_code == 200, delete_response.text
    assert upload_response.status_code == 404, upload_response.text
    assert (
        postgres_client.get(
            f"/projects/{project_id}",
            headers=postgres_admin_auth_headers,
        ).status_code
        == 404
    )
    assert not store_called.is_set()
    _assert_no_dataset_files(postgres_client, storage_root=storage_root)


def test_file_delete_then_project_delete_waits_for_commit_and_cleans_blob(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, dataset_id = _create_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        label="File delete versus project delete",
    )
    storage = postgres_client.app.state.file_storage_backend
    storage_root = Path(storage.base_path)
    upload_response = postgres_client.post(
        f"/datasets/{dataset_id}/files",
        files={
            "file": (
                "file-delete-project-race.bin",
                b"file-delete-project-race",
                "application/octet-stream",
            )
        },
        headers=postgres_admin_auth_headers,
    )
    assert upload_response.status_code == 201, upload_response.text
    file_id = UUID(upload_response.json()["data"]["file_id"])
    assert _count_suffix(storage_root, ".bin") == 1
    assert _count_suffix(storage_root, ".json") == 1

    file_delete_ready = Event()
    release_file_delete = Event()
    project_delete_entered_lock = Event()
    blob_deleted = Event()
    backend_pids: dict[str, int] = {}
    deleted_storage_ids: list[UUID] = []

    original_file_lock = SQLAlchemyLabTrackerRepository.lock_dataset_file_mutation
    original_project_lock = SQLAlchemyLabTrackerRepository.lock_project_deletion
    original_file_delete = DatasetFileCommands.delete
    original_storage_delete = storage.delete

    def observed_file_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
        locked_dataset_id: UUID,
    ) -> None:
        backend_pids["file_delete"] = _backend_pid(repository)
        original_file_lock(repository, locked_project_id, locked_dataset_id)

    def held_file_delete(
        commands: DatasetFileCommands,
        locked_dataset_id: UUID,
        locked_file_id: UUID,
        *,
        actor: Any,
    ) -> Any:
        result = original_file_delete(
            commands,
            locked_dataset_id,
            locked_file_id,
            actor=actor,
        )
        file_delete_ready.set()
        if not release_file_delete.wait(timeout=20):
            raise RuntimeError("Timed out holding the file deletion before commit.")
        return result

    def observed_project_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
    ) -> None:
        backend_pids["project_delete"] = _backend_pid(repository)
        project_delete_entered_lock.set()
        original_project_lock(repository, locked_project_id)

    def observed_storage_delete(storage_id: UUID) -> None:
        deleted_storage_ids.append(storage_id)
        original_storage_delete(storage_id)
        blob_deleted.set()

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_dataset_file_mutation",
        observed_file_lock,
    )
    monkeypatch.setattr(DatasetFileCommands, "delete", held_file_delete)
    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_project_deletion",
        observed_project_lock,
    )
    monkeypatch.setattr(storage, "delete", observed_storage_delete)

    with ThreadPoolExecutor(max_workers=2) as executor:
        file_delete_future = executor.submit(
            postgres_client.delete,
            f"/datasets/{dataset_id}/files/{file_id}",
            headers=postgres_admin_auth_headers,
        )
        assert file_delete_ready.wait(timeout=10)
        assert not blob_deleted.is_set()
        assert _count_suffix(storage_root, ".bin") == 1
        assert _count_suffix(storage_root, ".json") == 1

        project_delete_future = executor.submit(
            postgres_client.delete,
            f"/projects/{project_id}",
            headers=postgres_admin_auth_headers,
        )
        try:
            assert project_delete_entered_lock.wait(timeout=10)
            assert backend_pids["file_delete"] != backend_pids["project_delete"]
            _wait_until_blocked(
                postgres_client,
                blocked_pid=backend_pids["project_delete"],
                expected_blocker_pid=backend_pids["file_delete"],
            )
            assert not blob_deleted.is_set()
        finally:
            release_file_delete.set()
        file_delete_response = _future_result(file_delete_future)
        project_delete_response = _future_result(project_delete_future)

    assert file_delete_response.status_code == 200, file_delete_response.text
    assert project_delete_response.status_code == 200, project_delete_response.text
    assert blob_deleted.is_set()
    assert len(deleted_storage_ids) == 1
    assert (
        postgres_client.get(
            f"/projects/{project_id}",
            headers=postgres_admin_auth_headers,
        ).status_code
        == 404
    )
    _assert_no_dataset_files(postgres_client, storage_root=storage_root)


def test_dataset_delete_rollback_preserves_blob_and_retry_cleans_it(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, dataset_id = _create_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        label="Dataset delete rollback",
    )
    content = b"rollback-preserves-this-blob"
    upload_response = postgres_client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("rollback.bin", content, "application/octet-stream")},
        headers=postgres_admin_auth_headers,
    )
    assert upload_response.status_code == 201, upload_response.text
    file_id = upload_response.json()["data"]["file_id"]
    storage_root = Path(postgres_client.app.state.file_storage_backend.base_path)
    assert _count_suffix(storage_root, ".bin") == 1
    assert _count_suffix(storage_root, ".json") == 1

    original_commit = Session.commit
    failed = False

    def fail_once(session: Session, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("forced PostgreSQL commit failure")
        return original_commit(session, *args, **kwargs)

    monkeypatch.setattr(Session, "commit", fail_once)
    monkeypatch.setattr(
        postgres_client._transport,  # noqa: SLF001
        "raise_server_exceptions",
        False,
    )

    failed_delete = postgres_client.delete(
        f"/datasets/{dataset_id}",
        headers=postgres_admin_auth_headers,
    )
    assert failed_delete.status_code == 500
    assert failed, "Dataset DELETE returned before exercising the injected commit failure."
    monkeypatch.setattr(
        postgres_client._transport,  # noqa: SLF001
        "raise_server_exceptions",
        True,
    )
    assert _count_suffix(storage_root, ".bin") == 1
    assert _count_suffix(storage_root, ".json") == 1
    download = postgres_client.get(
        f"/datasets/{dataset_id}/files/{file_id}/download",
        headers=postgres_admin_auth_headers,
    )
    assert download.status_code == 200, download.text
    assert download.content == content

    retry = postgres_client.delete(
        f"/datasets/{dataset_id}",
        headers=postgres_admin_auth_headers,
    )
    assert retry.status_code == 200, retry.text
    _assert_no_dataset_files(postgres_client, storage_root=storage_root)


def test_dataset_deletion_does_not_block_file_writes_to_a_sibling_dataset(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, first_dataset_id = _create_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        label="Sibling dataset locks",
    )
    question_response = postgres_client.post(
        "/questions",
        json={
            "project_id": str(project_id),
            "text": "Sibling dataset question",
            "question_type": "descriptive",
        },
        headers=postgres_admin_auth_headers,
    )
    assert question_response.status_code == 201, question_response.text
    dataset_response = postgres_client.post(
        "/datasets",
        json={
            "project_id": str(project_id),
            "primary_question_id": question_response.json()["data"]["question_id"],
        },
        headers=postgres_admin_auth_headers,
    )
    assert dataset_response.status_code == 201, dataset_response.text
    second_dataset_id = UUID(dataset_response.json()["data"]["dataset_id"])

    original_delete_lock = SQLAlchemyLabTrackerRepository.lock_dataset_deletion
    first_locked = Event()
    release_first = Event()

    def hold_first_dataset(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
        locked_dataset_id: UUID,
    ) -> None:
        original_delete_lock(repository, locked_project_id, locked_dataset_id)
        if locked_dataset_id == first_dataset_id:
            first_locked.set()
            if not release_first.wait(timeout=20):
                raise RuntimeError("Timed out holding the first dataset lock.")

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_dataset_deletion",
        hold_first_dataset,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        delete_future = executor.submit(
            postgres_client.delete,
            f"/datasets/{first_dataset_id}",
            headers=postgres_admin_auth_headers,
        )
        assert first_locked.wait(timeout=10)
        upload_future = executor.submit(
            postgres_client.post,
            f"/datasets/{second_dataset_id}/files",
            files={
                "file": (
                    "sibling.bin",
                    b"sibling-dataset-remains-concurrent",
                    "application/octet-stream",
                )
            },
            headers=postgres_admin_auth_headers,
        )
        try:
            upload_response = _future_result(upload_future)
        finally:
            release_first.set()
        delete_response = _future_result(delete_future)

    assert upload_response.status_code == 201, upload_response.text
    assert delete_response.status_code == 200, delete_response.text
