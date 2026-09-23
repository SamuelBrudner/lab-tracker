"""A metadata PATCH must not revert a concurrently committed upload (M72)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from lab_tracker.application import file_commands as visualization_file_commands

pytestmark = pytest.mark.postgres


def _post(client: TestClient, headers: dict[str, str], path: str, payload: dict) -> dict:
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _create_visualization(client: TestClient, headers: dict[str, str]) -> str:
    project_id = _post(client, headers, "/projects", {"name": "PATCH vs upload"})["project_id"]
    question_id = _post(
        client,
        headers,
        "/questions",
        {
            "project_id": project_id,
            "text": "Does a caption edit keep the new figure?",
            "question_type": "descriptive",
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
            "method_hash": "patch-race-method",
            "code_version": "patch-race-code",
        },
    )["analysis_id"]
    return _post(
        client,
        headers,
        "/visualizations",
        {"analysis_id": analysis_id, "viz_type": "figure", "file_path": "figures/race.png"},
    )["viz_id"]


def _wait_until_blocked_by(client: TestClient, blocker_pid: int) -> None:
    deadline = monotonic() + 10
    poll = Event()
    while monotonic() < deadline:
        with client.app.state.db_engine.connect() as connection:
            blocked = connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE :blocker = ANY(pg_blocking_pids(pid))"
                ),
                {"blocker": blocker_pid},
            )
        if blocked:
            return
        poll.wait(timeout=0.01)
    pytest.fail(f"No backend was blocked by the upload backend {blocker_pid}.")


def test_postgres_caption_patch_waits_for_upload_and_keeps_the_new_asset(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = postgres_client
    headers = postgres_admin_auth_headers
    viz_id = _create_visualization(client, headers)
    original = client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("original.png", b"original-figure", "image/png")},
        headers=headers,
    )
    assert original.status_code == 201, original.text
    original_storage_id = original.json()["data"]["asset"]["storage_id"]

    original_lock = visualization_file_commands.locked_visualization_row
    upload_locked = Event()
    release_upload = Event()
    upload_pid: list[int] = []

    def held_lock(db_session, visualization_id):  # noqa: ANN001, ANN202
        row = original_lock(db_session, visualization_id)
        upload_pid.append(int(db_session.scalar(text("SELECT pg_backend_pid()"))))
        upload_locked.set()
        if not release_upload.wait(timeout=20):
            raise RuntimeError("Timed out holding the upload row lock.")
        return row

    monkeypatch.setattr(visualization_file_commands, "locked_visualization_row", held_lock)

    def upload():
        return client.post(
            f"/visualizations/{viz_id}/file",
            files={"file": ("replacement.png", b"replacement-figure", "image/png")},
            headers=headers,
        )

    def patch_caption():
        return client.patch(
            f"/visualizations/{viz_id}",
            json={"caption": "Edited while a new figure was uploading."},
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        upload_future = executor.submit(upload)
        assert upload_locked.wait(timeout=10)
        patch_future = executor.submit(patch_caption)
        try:
            _wait_until_blocked_by(client, upload_pid[0])
        finally:
            release_upload.set()
        uploaded = upload_future.result(timeout=20)
        patched = patch_future.result(timeout=20)

    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["meta"] == {"asset_outcome": "replaced"}
    new_asset = uploaded.json()["data"]["asset"]
    assert new_asset["storage_id"] != original_storage_id
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["asset"] == new_asset

    fetched = client.get(f"/visualizations/{viz_id}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["data"]["caption"] == "Edited while a new figure was uploading."
    assert fetched.json()["data"]["asset"] == new_asset
    download = client.get(f"/visualizations/{viz_id}/file/download", headers=headers)
    assert download.status_code == 200, download.text
    assert download.content == b"replacement-figure"
