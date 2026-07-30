from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from lab_tracker.auth import AuthContext, PrincipalType, Role
from lab_tracker.collection_db_models import (
    AcquisitionCollectionCaptureModel,
    AcquisitionCollectionManifestModel,
    AcquisitionCollectionModel,
    AcquisitionCollectionSnapshotModel,
)
from lab_tracker.services.collection_service import _capture_principal_instance_id

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
        observed_at="2026-07-24T12:30:00Z",
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
    assert history.json()["data"][0]["snapshot_id"] == snapshot_id
    assert history.json()["data"][0]["observed_at"] == "2026-07-24T13:00:00Z"

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
    assert (
        download.content
        == json.dumps(
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
    )

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


def test_deleting_session_cascades_collection_rows(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    session_id = _create_operational_session(client, admin_auth_headers)
    captured = _capture(
        client,
        admin_auth_headers,
        session_id=session_id,
        client_capture_id="capture-before-session-delete",
        observed_at="2026-07-24T12:00:00Z",
        complete=True,
        members=[
            {
                "path": "trial/data.bin",
                "checksum": _A_HASH,
                "size_bytes": 1,
            }
        ],
    )
    assert captured.status_code == 201

    deleted = client.delete(
        f"/sessions/{session_id}",
        headers=admin_auth_headers,
    )
    assert deleted.status_code == 200, deleted.text

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
    assert counts == [0, 0, 0, 0]


def test_equal_observation_ties_use_the_newest_capture_receipt(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    session_id = _create_operational_session(client, admin_auth_headers)
    members = [
        {
            "path": "trial/data.bin",
            "checksum": _A_HASH,
            "size_bytes": 1,
        }
    ]
    first = _capture(
        client,
        admin_auth_headers,
        session_id=session_id,
        client_capture_id="equal-time-first",
        observed_at="2026-07-24T12:00:00Z",
        complete=True,
        members=members,
    )
    assert first.status_code == 201
    first_snapshot_id = first.json()["data"]["snapshot_id"]

    second = client.post(
        f"/sessions/{session_id}/collections/trials/snapshots",
        json={
            "client_capture_id": "equal-time-newest",
            "observed_at": "2026-07-24T12:00:00Z",
            "source_provider": "filesystem",
            "source_uri": "file:///newer-receipt",
            "complete": True,
            "manifest": {"schema_version": 1, "members": members},
        },
        headers=admin_auth_headers,
    )
    assert second.status_code == 201
    assert second.json()["meta"]["current_pointer_changed"] is True

    current_collection = client.get(
        f"/sessions/{session_id}/collections",
        headers=admin_auth_headers,
    ).json()["data"][0]
    assert current_collection["current_snapshot"]["client_capture_id"] == "equal-time-newest"
    assert current_collection["current_snapshot"]["source_uri"] == "file:///newer-receipt"
    current_snapshot = client.get(
        f"/collection-snapshots/{first_snapshot_id}",
        headers=admin_auth_headers,
    )
    assert current_snapshot.status_code == 200
    assert current_snapshot.json()["data"]["client_capture_id"] == "equal-time-newest"
    assert current_snapshot.json()["data"]["source_uri"] == "file:///newer-receipt"

    newer_snapshot = _capture(
        client,
        admin_auth_headers,
        session_id=session_id,
        client_capture_id="later-snapshot",
        observed_at="2026-07-24T13:00:00Z",
        complete=True,
        members=[
            {
                "path": "trial/other.bin",
                "checksum": _B_HASH,
                "size_bytes": 2,
            }
        ],
    )
    assert newer_snapshot.status_code == 201

    collections = client.get(
        f"/sessions/{session_id}/collections",
        headers=admin_auth_headers,
    ).json()["data"]
    history = client.get(
        f"/collections/{collections[0]['collection_id']}/snapshots",
        headers=admin_auth_headers,
    )
    assert history.status_code == 200
    historical = next(
        item for item in history.json()["data"] if item["snapshot_id"] == first_snapshot_id
    )
    assert historical["client_capture_id"] == "equal-time-newest"
    assert historical["source_uri"] == "file:///newer-receipt"


def test_database_rejects_cross_collection_capture_and_current_pointers(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    first_session_id = _create_operational_session(client, admin_auth_headers)
    second_session_id = _create_operational_session(client, admin_auth_headers)
    for session_id, suffix, checksum in (
        (first_session_id, "first", _A_HASH),
        (second_session_id, "second", _B_HASH),
    ):
        response = _capture(
            client,
            admin_auth_headers,
            session_id=session_id,
            client_capture_id=f"capture-{suffix}",
            observed_at="2026-07-24T12:00:00Z",
            complete=True,
            members=[
                {
                    "path": f"{suffix}.bin",
                    "checksum": checksum,
                    "size_bytes": 1,
                }
            ],
        )
        assert response.status_code == 201
    additional_first_capture = _capture(
        client,
        admin_auth_headers,
        session_id=first_session_id,
        client_capture_id="capture-first-new",
        observed_at="2026-07-24T13:00:00Z",
        complete=True,
        members=[
            {
                "path": "first-new.bin",
                "checksum": _C_HASH,
                "size_bytes": 2,
            }
        ],
    )
    assert additional_first_capture.status_code == 201

    with client.app.state.db_session_factory() as db_session:
        first_collection = db_session.scalar(
            select(AcquisitionCollectionModel).where(
                AcquisitionCollectionModel.session_id == first_session_id
            )
        )
        second_collection = db_session.scalar(
            select(AcquisitionCollectionModel).where(
                AcquisitionCollectionModel.session_id == second_session_id
            )
        )
        assert first_collection is not None
        assert second_collection is not None
        first_captures = list(
            db_session.scalars(
                select(AcquisitionCollectionCaptureModel)
                .where(
                    AcquisitionCollectionCaptureModel.collection_id
                    == str(first_collection.collection_id)
                )
                .order_by(AcquisitionCollectionCaptureModel.observed_at)
            )
        )
        second_capture = db_session.scalar(
            select(AcquisitionCollectionCaptureModel).where(
                AcquisitionCollectionCaptureModel.collection_id
                == str(second_collection.collection_id)
            )
        )
        assert len(first_captures) == 2
        first_capture, first_new_capture = first_captures
        assert second_capture is not None

        with pytest.raises(IntegrityError):
            db_session.execute(
                update(AcquisitionCollectionCaptureModel)
                .where(
                    AcquisitionCollectionCaptureModel.capture_id == str(first_capture.capture_id)
                )
                .values(snapshot_id=second_capture.snapshot_id)
            )
            db_session.commit()
        db_session.rollback()

        with pytest.raises(IntegrityError):
            db_session.execute(
                update(AcquisitionCollectionModel)
                .where(
                    AcquisitionCollectionModel.collection_id == str(first_collection.collection_id)
                )
                .values(
                    current_snapshot_id=second_capture.snapshot_id,
                    current_capture_id=second_capture.capture_id,
                )
            )
            db_session.commit()
        db_session.rollback()

        with pytest.raises(IntegrityError):
            db_session.execute(
                update(AcquisitionCollectionModel)
                .where(
                    AcquisitionCollectionModel.collection_id == str(first_collection.collection_id)
                )
                .values(
                    current_observed_at=datetime(
                        2099,
                        1,
                        1,
                        tzinfo=timezone.utc,
                    )
                )
            )
            db_session.commit()
        db_session.rollback()

        with pytest.raises(IntegrityError):
            db_session.execute(
                update(AcquisitionCollectionModel)
                .where(
                    AcquisitionCollectionModel.collection_id == str(first_collection.collection_id)
                )
                .values(
                    current_snapshot_id=first_capture.snapshot_id,
                    current_capture_id=first_new_capture.capture_id,
                )
            )
            db_session.commit()


def test_capture_principal_instance_ids_do_not_misidentify_service_or_system() -> None:
    user_id = UUID(int=1)
    device_token_id = UUID(int=2)

    assert _capture_principal_instance_id(AuthContext(user_id=user_id, role=Role.ADMIN)) == user_id
    assert (
        _capture_principal_instance_id(
            AuthContext(
                user_id=user_id,
                role=Role.ADMIN,
                principal_type=PrincipalType.DEVICE,
                device_token_id=device_token_id,
            )
        )
        == device_token_id
    )
    for principal_type in (PrincipalType.SERVICE, PrincipalType.SYSTEM):
        assert (
            _capture_principal_instance_id(
                AuthContext(
                    user_id=user_id,
                    role=Role.ADMIN,
                    principal_type=principal_type,
                )
            )
            is None
        )
