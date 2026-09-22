"""Visualization read-modify-writes serialize on file-backed SQLite too (M72).

Legacy pysqlite transaction control runs a SELECT outside any write
transaction, so ``FOR UPDATE`` alone would let a locked read go stale before
the write. These tests pause one request right after its locked read and let
a second request race it through the real HTTP stack.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from lab_tracker.application import file_commands as visualization_file_commands
from lab_tracker.sqlalchemy_repository_parts.analyses import (
    SQLAlchemyVisualizationRepository,
)

# How long the paused request keeps its lock while the competitor runs. A
# competitor that is not serialized finishes well within this window; one
# that is serialized waits on SQLite's busy timeout (5 s) and resumes after.
_COMPETITOR_WINDOW_SECONDS = 1.5


def _post(client: TestClient, headers: dict[str, str], path: str, payload: dict) -> dict:
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _create_visualization(client: TestClient, headers: dict[str, str]) -> str:
    project_id = _post(client, headers, "/projects", {"name": "SQLite viz race"})["project_id"]
    question_id = _post(
        client,
        headers,
        "/questions",
        {
            "project_id": project_id,
            "text": "Does SQLite serialize visualization writes?",
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
            "method_hash": "sqlite-race-method",
            "code_version": "sqlite-race-code",
        },
    )["analysis_id"]
    return _post(
        client,
        headers,
        "/visualizations",
        {"analysis_id": analysis_id, "viz_type": "figure", "file_path": "figures/race.png"},
    )["viz_id"]


def _upload_original(client: TestClient, headers: dict[str, str], viz_id: str) -> str:
    response = client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("original.png", b"original-figure", "image/png")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["asset"]["storage_id"])


def _stored_blob_count(client: TestClient) -> int:
    root = Path(client.app.state.file_storage_backend.base_path)
    return sum(1 for _ in root.rglob("*.bin"))


def _race(
    paused: Callable[[], httpx.Response],
    competitor: Callable[[], httpx.Response],
    *,
    paused_after_locked_read: Event,
    release: Event,
) -> tuple[httpx.Response, httpx.Response]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        paused_future: Future[httpx.Response] = executor.submit(paused)
        try:
            assert paused_after_locked_read.wait(timeout=10)
            competitor_future: Future[httpx.Response] = executor.submit(competitor)
            wait([competitor_future], timeout=_COMPETITOR_WINDOW_SECONDS)
        finally:
            release.set()
        return paused_future.result(timeout=20), competitor_future.result(timeout=20)


def _pause_first_call(
    original: Callable[..., Any],
    *,
    paused_after_locked_read: Event,
    release: Event,
) -> Callable[..., Any]:
    calls: list[int] = []

    def paused_call(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            paused_after_locked_read.set()
            if not release.wait(timeout=20):
                raise RuntimeError("Timed out holding the visualization lock.")
        return result

    return paused_call


def test_sqlite_caption_patch_serializes_with_replacement_upload(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = admin_auth_headers
    viz_id = _create_visualization(client, headers)
    original_storage_id = _upload_original(client, headers, viz_id)

    paused_after_locked_read = Event()
    release = Event()
    monkeypatch.setattr(
        SQLAlchemyVisualizationRepository,
        "get_for_update",
        _pause_first_call(
            SQLAlchemyVisualizationRepository.get_for_update,
            paused_after_locked_read=paused_after_locked_read,
            release=release,
        ),
    )

    patched, uploaded = _race(
        lambda: client.patch(
            f"/visualizations/{viz_id}",
            json={"caption": "Edited while a new figure was uploading."},
            headers=headers,
        ),
        lambda: client.post(
            f"/visualizations/{viz_id}/file",
            files={"file": ("replacement.png", b"replacement-figure", "image/png")},
            headers=headers,
        ),
        paused_after_locked_read=paused_after_locked_read,
        release=release,
    )

    assert patched.status_code == 200, patched.text
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["meta"] == {"asset_outcome": "replaced"}
    new_asset = uploaded.json()["data"]["asset"]
    assert new_asset["storage_id"] != original_storage_id

    fetched = client.get(f"/visualizations/{viz_id}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["data"]["caption"] == "Edited while a new figure was uploading."
    assert fetched.json()["data"]["asset"] == new_asset
    download = client.get(f"/visualizations/{viz_id}/file/download", headers=headers)
    assert download.status_code == 200, download.text
    assert download.content == b"replacement-figure"
    assert _stored_blob_count(client) == 1


def test_sqlite_conditional_uploads_serialize_and_reject_the_stale_loser(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = admin_auth_headers
    viz_id = _create_visualization(client, headers)
    original_storage_id = _upload_original(client, headers, viz_id)

    paused_after_locked_read = Event()
    release = Event()
    monkeypatch.setattr(
        visualization_file_commands,
        "locked_visualization_row",
        _pause_first_call(
            visualization_file_commands.locked_visualization_row,
            paused_after_locked_read=paused_after_locked_read,
            release=release,
        ),
    )

    def conditional_upload(name: str, content: bytes) -> Callable[[], httpx.Response]:
        return lambda: client.post(
            f"/visualizations/{viz_id}/file",
            files={"file": (name, content, "image/png")},
            data={
                "checksum_sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": str(len(content)),
                "expected_current_storage_id": original_storage_id,
            },
            headers=headers,
        )

    first, second = _race(
        conditional_upload("first.png", b"first-replacement"),
        conditional_upload("second.png", b"second-replacement"),
        paused_after_locked_read=paused_after_locked_read,
        release=release,
    )

    assert first.status_code == 201, first.text
    assert first.json()["meta"] == {"asset_outcome": "replaced"}
    assert second.status_code == 409, second.text
    fetched = client.get(f"/visualizations/{viz_id}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["data"]["asset"] == first.json()["data"]["asset"]
    download = client.get(f"/visualizations/{viz_id}/file/download", headers=headers)
    assert download.status_code == 200, download.text
    assert download.content == b"first-replacement"
    assert _stored_blob_count(client) == 1


def _updated_at(client: TestClient, headers: dict[str, str], viz_id: str) -> str:
    response = client.get(f"/visualizations/{viz_id}", headers=headers)
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["updated_at"])


def test_sqlite_write_fence_leaves_noop_patch_updated_at_unchanged(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    headers = admin_auth_headers
    viz_id = _create_visualization(client, headers)
    patch = {"caption": "Unchanged caption."}
    first = client.patch(f"/visualizations/{viz_id}", json=patch, headers=headers)
    assert first.status_code == 200, first.text
    before = _updated_at(client, headers, viz_id)
    time.sleep(0.05)

    repeated = client.patch(f"/visualizations/{viz_id}", json=patch, headers=headers)

    assert repeated.status_code == 200, repeated.text
    assert _updated_at(client, headers, viz_id) == before


def test_sqlite_write_fence_leaves_reused_upload_updated_at_unchanged(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    headers = admin_auth_headers
    viz_id = _create_visualization(client, headers)
    content = b"idempotent-figure"
    data = {
        "checksum_sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": str(len(content)),
    }

    def upload() -> httpx.Response:
        return client.post(
            f"/visualizations/{viz_id}/file",
            files={"file": ("figure.png", content, "image/png")},
            data=data,
            headers=headers,
        )

    first = upload()
    assert first.status_code == 201, first.text
    before = _updated_at(client, headers, viz_id)
    time.sleep(0.05)

    retried = upload()

    assert retried.status_code == 200, retried.text
    assert retried.json()["meta"] == {"asset_outcome": "reused"}
    assert _updated_at(client, headers, viz_id) == before
