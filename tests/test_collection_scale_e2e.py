from __future__ import annotations

from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select

import lab_tracker_client.watch as watch_capture
from lab_tracker.collection_db_models import (
    AcquisitionCollectionCaptureModel,
    AcquisitionCollectionManifestModel,
    AcquisitionCollectionModel,
    AcquisitionCollectionSnapshotModel,
    DatasetCollectionSnapshotLinkModel,
)
from lab_tracker_client import LabTracker
from lab_tracker_client.watch import (
    SINK_ACQUISITION_COLLECTION,
    FileObservation,
    init_config,
    read_event,
    scan_watch,
    sync_outbox,
)


def _create_project(
    client: TestClient,
    headers: dict[str, str],
) -> str:
    response = client.post(
        "/projects",
        json={"name": "10,000-member collection certification"},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _create_question(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
) -> str:
    response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Can a high-cardinality acquisition promote as one Dataset?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["question_id"]


def _create_operational_session(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
) -> str:
    response = client.post(
        "/sessions",
        json={
            "project_id": project_id,
            "session_type": "operational",
        },
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["session_id"]


def _collection_record_counts(client: TestClient) -> list[int]:
    with client.app.state.db_session_factory() as db_session:
        return [
            int(db_session.scalar(select(func.count()).select_from(model)) or 0)
            for model in (
                AcquisitionCollectionModel,
                AcquisitionCollectionSnapshotModel,
                AcquisitionCollectionManifestModel,
                AcquisitionCollectionCaptureModel,
                DatasetCollectionSnapshotLinkModel,
            )
        ]


def test_ten_thousand_member_watch_capture_promotes_with_constant_records(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Certify the complete high-cardinality path without 10,000 graph rows."""

    project_id = _create_project(client, admin_auth_headers)
    question_id = _create_question(
        client,
        admin_auth_headers,
        project_id=project_id,
    )
    session_id = _create_operational_session(
        client,
        admin_auth_headers,
        project_id=project_id,
    )

    monkeypatch.chdir(tmp_path)
    config = init_config()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    paths = [outputs / f"trial-{index:05d}.bin" for index in range(10_000)]
    monkeypatch.setattr(
        watch_capture,
        "discover_files",
        lambda *args, **kwargs: paths,
    )

    def fake_observation(
        path: Path,
        *,
        root: Path,
        source_external_id: str | None = None,
    ) -> FileObservation:
        del root, source_external_id
        index = int(path.stem.removeprefix("trial-"))
        return FileObservation(
            path=path,
            root=outputs,
            relative_path=path.name,
            source_uri=path.as_uri(),
            source_external_id=f"trial-{index}",
            content_hash=f"{index:064x}",
            size_bytes=index,
            mtime=1.0,
        )

    monkeypatch.setattr(watch_capture, "observe_file", fake_observation)
    preverified_event_ids: set[str] = set()
    scanned = scan_watch(
        config,
        mode="files",
        root=outputs,
        sink=SINK_ACQUISITION_COLLECTION,
        session_id=session_id,
        collection_key="ten-thousand-trials",
        complete=True,
        verified_event_ids=preverified_event_ids,
    )
    event_files = list(config.outbox_path().glob("*.json"))
    assert scanned["matched"] == 10_000
    assert scanned["processed"] == 10_000
    assert len(scanned["imported"]) == 1
    assert len(event_files) == 1
    assert (
        len(read_event(event_files[0])["payload"]["manifest"]["members"])
        == 10_000
    )

    watch_requests: list[tuple[str, str]] = []

    def route_to_test_client(request: httpx.Request) -> httpx.Response:
        watch_requests.append((request.method, request.url.path))
        forwarded_headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"content-length", "host"}
        }
        response = client.request(
            request.method,
            request.url.path,
            params=list(request.url.params.multi_items()),
            content=request.content,
            headers=forwarded_headers,
        )
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers=response.headers,
        )

    access_token = admin_auth_headers["Authorization"].removeprefix("Bearer ")
    with LabTracker(
        base_url="http://testserver",
        access_token=access_token,
        transport=httpx.MockTransport(route_to_test_client),
    ) as lab_tracker:
        synced = sync_outbox(
            lab_tracker,
            config,
            preverified_event_ids=preverified_event_ids,
        )

    assert synced["errors"] == []
    assert len(synced["results"]) == 1
    assert len(
        [
            request
            for request in watch_requests
            if request[0] == "POST"
        ]
    ) == 1
    assert watch_requests == [
        ("GET", "/schema/describe"),
        (
            "POST",
            f"/sessions/{session_id}/collections/"
            "ten-thousand-trials/snapshots",
        ),
    ]
    assert _collection_record_counts(client) == [1, 1, 1, 1, 0]

    with client.app.state.db_session_factory() as db_session:
        table_names = set(inspect(db_session.get_bind()).get_table_names())
    assert "acquisition_collection_members" not in table_names

    promoted = client.post(
        f"/sessions/{session_id}/promote-to-dataset",
        json={"primary_question_id": question_id},
        headers=admin_auth_headers,
    )
    assert promoted.status_code == 201
    dataset = promoted.json()["data"]
    assert dataset["status"] == "committed"
    assert dataset["commit_manifest"]["files"] == []
    assert len(dataset["commit_manifest"]["collection_snapshots"]) == 1
    collection_reference = dataset["commit_manifest"]["collection_snapshots"][0]
    assert collection_reference["collection_key"] == "ten-thousand-trials"
    assert collection_reference["member_count"] == 10_000
    assert collection_reference["total_size_bytes"] == sum(range(10_000))

    assert _collection_record_counts(client) == [1, 1, 1, 1, 1]
    summary = client.get(
        "/datasets/summaries",
        params={"dataset_id": dataset["dataset_id"], "limit": 1},
        headers=admin_auth_headers,
    )
    assert summary.status_code == 200
    summary_data = summary.json()["data"][0]
    assert summary_data["collection_count"] == 1
    assert summary_data["collection_member_count"] == 10_000
    assert summary_data["file_count"] == 0
