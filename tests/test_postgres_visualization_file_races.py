"""Real PostgreSQL race coverage for conditional visualization attachments."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock

import pytest
from fastapi.testclient import TestClient

from lab_tracker.routes import visualizations as visualization_routes

pytestmark = pytest.mark.postgres


def _count_suffix(root: Path, suffix: str) -> int:
    if not root.exists():
        return 0
    return sum(1 for _ in root.rglob(f"*{suffix}"))


def _create_visualization(
    client: TestClient,
    headers: dict[str, str],
) -> str:
    project = client.post(
        "/projects",
        json={"name": "Concurrent visualization upload"},
        headers=headers,
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["data"]["project_id"]
    question = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Do concurrent attachment retries converge?",
            "question_type": "descriptive",
        },
        headers=headers,
    )
    assert question.status_code == 201, question.text
    dataset = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question.json()["data"]["question_id"],
        },
        headers=headers,
    )
    assert dataset.status_code == 201, dataset.text
    analysis = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset.json()["data"]["dataset_id"]],
            "method_hash": "attachment-race-method",
            "code_version": "attachment-race-code",
        },
        headers=headers,
    )
    assert analysis.status_code == 201, analysis.text
    visualization = client.post(
        "/visualizations",
        json={
            "analysis_id": analysis.json()["data"]["analysis_id"],
            "viz_type": "figure",
            "file_path": "figures/concurrent.png",
        },
        headers=headers,
    )
    assert visualization.status_code == 201, visualization.text
    return visualization.json()["data"]["viz_id"]


def _synchronize_row_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    original_lock = visualization_routes._locked_visualization_row
    before_lock = Barrier(2)

    def synchronized_lock(db_session, visualization_id):  # noqa: ANN001
        before_lock.wait(timeout=10)
        return original_lock(db_session, visualization_id)

    monkeypatch.setattr(
        visualization_routes,
        "_locked_visualization_row",
        synchronized_lock,
    )


def test_postgres_identical_conditional_uploads_reuse_one_storage_object(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viz_id = _create_visualization(
        postgres_client,
        postgres_admin_auth_headers,
    )
    storage_root = Path(postgres_client.app.state.file_storage_backend.base_path)
    content = b"identical-concurrent-visualization"
    checksum = hashlib.sha256(content).hexdigest()
    form = {
        "checksum_sha256": checksum,
        "size_bytes": str(len(content)),
        "expected_current_storage_id": "absent",
    }

    _synchronize_row_lock(monkeypatch)

    def upload(_index: int):
        return postgres_client.post(
            f"/visualizations/{viz_id}/file",
            files={"file": ("concurrent.png", content, "image/png")},
            data=form,
            headers=postgres_admin_auth_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(upload, range(2)))

    assert sorted(response.status_code for response in responses) == [200, 201]
    assert sorted(response.json()["meta"]["asset_outcome"] for response in responses) == [
        "created",
        "reused",
    ]
    assets = [response.json()["data"]["asset"] for response in responses]
    assert assets[0] == assets[1]
    assert assets[0]["checksum"] == checksum
    assert _count_suffix(storage_root, ".bin") == 1
    assert _count_suffix(storage_root, ".json") == 1

    download = postgres_client.get(
        f"/visualizations/{viz_id}/file/download",
        headers=postgres_admin_auth_headers,
    )
    assert download.status_code == 200
    assert download.content == content


def test_postgres_conditional_replacement_race_rejects_stale_loser_without_orphan(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viz_id = _create_visualization(
        postgres_client,
        postgres_admin_auth_headers,
    )
    storage_root = Path(postgres_client.app.state.file_storage_backend.base_path)
    original = postgres_client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("original.png", b"original", "image/png")},
        headers=postgres_admin_auth_headers,
    )
    assert original.status_code == 201, original.text
    original_storage_id = original.json()["data"]["asset"]["storage_id"]
    candidates = [
        ("replacement-a.png", b"replacement-a"),
        ("replacement-b.png", b"replacement-b"),
    ]
    _synchronize_row_lock(monkeypatch)

    def replace(candidate: tuple[str, bytes]):
        filename, content = candidate
        return postgres_client.post(
            f"/visualizations/{viz_id}/file",
            files={"file": (filename, content, "image/png")},
            data={
                "checksum_sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": str(len(content)),
                "expected_current_storage_id": original_storage_id,
            },
            headers=postgres_admin_auth_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(replace, candidates))

    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["error"]["code"] == "conflict"
    winner = next(response for response in responses if response.status_code == 201)
    assert winner.json()["meta"] == {"asset_outcome": "replaced"}
    fetched = postgres_client.get(
        f"/visualizations/{viz_id}",
        headers=postgres_admin_auth_headers,
    )
    assert fetched.json()["data"]["asset"] == winner.json()["data"]["asset"]
    assert fetched.json()["data"]["asset"]["storage_id"] != original_storage_id
    assert _count_suffix(storage_root, ".bin") == 1
    assert _count_suffix(storage_root, ".json") == 1


def test_postgres_upload_then_delete_serializes_and_cleans_committed_blob(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viz_id = _create_visualization(
        postgres_client,
        postgres_admin_auth_headers,
    )
    storage_backend = postgres_client.app.state.file_storage_backend
    storage_root = Path(storage_backend.base_path)
    content = b"upload-delete-race"
    checksum = hashlib.sha256(content).hexdigest()

    blob_stored = Event()
    delete_waiting_for_lock = Event()
    original_store_stream = storage_backend.store_stream

    def paused_store_stream(*args, **kwargs):  # noqa: ANN002, ANN003
        metadata = original_store_stream(*args, **kwargs)
        blob_stored.set()
        assert delete_waiting_for_lock.wait(timeout=10)
        return metadata

    monkeypatch.setattr(storage_backend, "store_stream", paused_store_stream)

    original_lock = visualization_routes._locked_visualization_row
    call_guard = Lock()
    lock_calls = 0

    def observed_lock(db_session, visualization_id):  # noqa: ANN001
        nonlocal lock_calls
        with call_guard:
            lock_calls += 1
            call_number = lock_calls
        if call_number == 2:
            delete_waiting_for_lock.set()
        return original_lock(db_session, visualization_id)

    monkeypatch.setattr(
        visualization_routes,
        "_locked_visualization_row",
        observed_lock,
    )

    def upload():
        return postgres_client.post(
            f"/visualizations/{viz_id}/file",
            files={"file": ("race.png", content, "image/png")},
            data={
                "checksum_sha256": checksum,
                "size_bytes": str(len(content)),
                "expected_current_storage_id": "absent",
            },
            headers=postgres_admin_auth_headers,
        )

    def delete():
        return postgres_client.delete(
            f"/visualizations/{viz_id}",
            headers=postgres_admin_auth_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        upload_future = executor.submit(upload)
        assert blob_stored.wait(timeout=10)
        delete_future = executor.submit(delete)
        upload_response = upload_future.result(timeout=15)
        delete_response = delete_future.result(timeout=15)

    assert upload_response.status_code == 201, upload_response.text
    assert delete_response.status_code == 200, delete_response.text
    assert (
        postgres_client.get(
            f"/visualizations/{viz_id}",
            headers=postgres_admin_auth_headers,
        ).status_code
        == 404
    )
    assert _count_suffix(storage_root, ".bin") == 0
    assert _count_suffix(storage_root, ".json") == 0
