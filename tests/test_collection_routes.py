from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from lab_tracker.collection_db_models import (
    AcquisitionCollectionCaptureModel,
    AcquisitionCollectionManifestModel,
    AcquisitionCollectionModel,
    AcquisitionCollectionSnapshotModel,
)

_A_HASH = "a" * 64
_B_HASH = "b" * 64
_C_HASH = "c" * 64


def _create_operational_session(
    client: TestClient,
    headers: dict[str, str],
) -> str:
    project = client.post(
        "/projects",
        json={"name": "Collection capture"},
        headers=headers,
    )
    assert project.status_code == 201
    session = client.post(
        "/sessions",
        json={
            "project_id": project.json()["data"]["project_id"],
            "session_type": "operational",
        },
        headers=headers,
    )
    assert session.status_code == 201
    return session.json()["data"]["session_id"]


def _capture(
    client: TestClient,
    headers: dict[str, str],
    *,
    session_id: str,
    client_capture_id: str,
    observed_at: str,
    complete: bool,
    members: list[dict[str, object]],
):
    return client.post(
        f"/sessions/{session_id}/collections/trials/snapshots",
        json={
            "client_capture_id": client_capture_id,
            "observed_at": observed_at,
            "source_provider": "filesystem",
            "source_uri": "file:///acquisition/run-001",
            "complete": complete,
            "manifest": {"schema_version": 1, "members": members},
        },
        headers=headers,
    )


def test_collection_capture_is_idempotent_ordered_and_lazily_read(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    session_id = _create_operational_session(client, admin_auth_headers)
    members = [
        {
            "path": "trial-0002/data.bin",
            "checksum": _B_HASH,
            "size_bytes": 2**31 + 7,
        },
        {
            "path": "trial-0001/data.bin",
            "checksum": _A_HASH,
            "size_bytes": 11,
        },
    ]
    first = _capture(
        client,
        admin_auth_headers,
        session_id=session_id,
        client_capture_id="capture-1",
        observed_at="2026-07-24T12:00:00Z",
        complete=False,
        members=members,
    )
    assert first.status_code == 201, first.text
    snapshot_id = first.json()["data"]["snapshot_id"]
    assert first.json()["data"]["member_count"] == 2
    assert first.json()["data"]["total_size_bytes"] == 2**31 + 18
    assert first.json()["meta"] == {
        "snapshot_reused": False,
        "current_pointer_changed": True,
    }

    replay = _capture(
        client,
        admin_auth_headers,
        session_id=session_id,
        client_capture_id="capture-1",
        observed_at="2026-07-24T12:00:00Z",
        complete=False,
        members=list(reversed(members)),
    )
    assert replay.status_code == 201
    assert replay.json()["data"]["snapshot_id"] == snapshot_id
    assert replay.json()["meta"]["snapshot_reused"] is True

    mismatched_replay = _capture(
        client,
        admin_auth_headers,
        session_id=session_id,
        client_capture_id="capture-1",
        observed_at="2026-07-24T12:00:01Z",
        complete=False,
        members=members,
    )
    assert mismatched_replay.status_code == 409

    sealed_rescan = _capture(
        client,
        admin_auth_headers,
        session_id=session_id,
        client_capture_id="capture-2",
        observed_at="2026-07-24T13:00:00Z",
        complete=True,
        members=members,
    )
    assert sealed_rescan.status_code == 201
    assert sealed_rescan.json()["data"]["snapshot_id"] == snapshot_id
    assert sealed_rescan.json()["data"]["complete"] is True
    assert sealed_rescan.json()["meta"] == {
        "snapshot_reused": True,
        "current_pointer_changed": True,
    }

    older = _capture(
        client,
        admin_auth_headers,
        session_id=session_id,
        client_capture_id="capture-offline-old",
        observed_at="2026-07-24T11:00:00Z",
        complete=True,
        members=[
            {
                "path": "trial-0000/data.bin",
                "checksum": _C_HASH,
                "size_bytes": 5,
            }
        ],
    )
    assert older.status_code == 201
    assert older.json()["data"]["snapshot_id"] != snapshot_id
    assert older.json()["meta"]["current_pointer_changed"] is False

    equal_time_conflict = _capture(
        client,
        admin_auth_headers,
        session_id=session_id,
        client_capture_id="capture-equal-conflict",
        observed_at="2026-07-24T13:00:00Z",
        complete=True,
        members=[
            {
                "path": "other.bin",
                "checksum": _C_HASH,
                "size_bytes": 1,
            }
        ],
    )
    assert equal_time_conflict.status_code == 409

    collections = client.get(
        f"/sessions/{session_id}/collections",
        headers=admin_auth_headers,
    )
    assert collections.status_code == 200
    summary = collections.json()["data"][0]
    assert summary["current_snapshot_id"] == snapshot_id
    assert summary["current_snapshot"]["complete"] is True
    assert "members" not in summary
    assert "members" not in summary["current_snapshot"]

    history = client.get(
        f"/collections/{summary['collection_id']}/snapshots",
        headers=admin_auth_headers,
    )
    assert history.status_code == 200
    assert history.json()["meta"]["total"] == 2

    member_page = client.get(
        f"/collection-snapshots/{snapshot_id}/members",
        params={"limit": 1, "offset": 1, "q": "trial-"},
        headers=admin_auth_headers,
    )
    assert member_page.status_code == 200
    assert member_page.json()["meta"] == {
        "limit": 1,
        "offset": 1,
        "total": 2,
    }
    assert member_page.json()["data"][0]["path"] == "trial-0002/data.bin"

    download = client.get(
        f"/collection-snapshots/{snapshot_id}/manifest",
        headers=admin_auth_headers,
    )
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.content == json.dumps(
        {
            "members": [
                {
                    "checksum": _A_HASH,
                    "path": "trial-0001/data.bin",
                    "size_bytes": 11,
                },
                {
                    "checksum": _B_HASH,
                    "path": "trial-0002/data.bin",
                    "size_bytes": 2**31 + 7,
                },
            ],
            "schema_version": 1,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    with client.app.state.db_session_factory() as db_session:
        counts = [
            db_session.scalar(select(func.count()).select_from(model))
            for model in (
                AcquisitionCollectionModel,
                AcquisitionCollectionSnapshotModel,
                AcquisitionCollectionManifestModel,
                AcquisitionCollectionCaptureModel,
            )
        ]
    assert counts == [1, 2, 2, 3]


def test_ten_thousand_members_create_constant_database_records(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    session_id = _create_operational_session(client, admin_auth_headers)
    members = [
        {
            "path": f"trial-{index:05d}/data.bin",
            "checksum": f"{index:064x}",
            "size_bytes": index,
        }
        for index in range(10_000)
    ]

    response = _capture(
        client,
        admin_auth_headers,
        session_id=session_id,
        client_capture_id="capture-10000",
        observed_at="2026-07-24T12:00:00Z",
        complete=True,
        members=members,
    )

    assert response.status_code == 201, response.text
    snapshot = response.json()["data"]
    assert snapshot["member_count"] == 10_000
    assert snapshot["total_size_bytes"] == sum(range(10_000))
    page = client.get(
        f"/collection-snapshots/{snapshot['snapshot_id']}/members",
        params={"limit": 100, "offset": 9_900},
        headers=admin_auth_headers,
    )
    assert page.status_code == 200
    assert page.json()["meta"]["total"] == 10_000
    assert len(page.json()["data"]) == 100

    with client.app.state.db_session_factory() as db_session:
        counts = [
            db_session.scalar(select(func.count()).select_from(model))
            for model in (
                AcquisitionCollectionModel,
                AcquisitionCollectionSnapshotModel,
                AcquisitionCollectionManifestModel,
                AcquisitionCollectionCaptureModel,
            )
        ]
    assert counts == [1, 1, 1, 1]
