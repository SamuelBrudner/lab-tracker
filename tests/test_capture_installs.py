"""Name capture machines whose Lab Tracker client is behind the server (GH #238)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from lab_tracker.client_release import ReleaseIdentity
from lab_tracker.db_models import NoteModel
from lab_tracker.models import CaptureInstallObservation
from lab_tracker.services import capture_install_service
from lab_tracker.services.capture_install_service import (
    summarize_capture_installs,
    watched_folder,
)

SERVER = ReleaseIdentity(version="0.5.0", revision="b" * 40)
INSTALL_A = "a" * 32
INSTALL_B = "b" * 32
INSTALL_C = "c" * 32
FLY_URI = "file:///Users/sam/data/fly_walking_data/run1/trace.csv"


@pytest.mark.parametrize(
    ("uri", "relative", "folder"),
    [
        (FLY_URI, "run1/trace.csv", "fly_walking_data"),
        ("file:///C:/lab/fly%20walking/run1/trace.csv", "run1/trace.csv", "fly walking"),
        ("file:///data/results/plot.png", "plot.png", "results"),
    ],
)
def test_watched_folder_is_the_file_uri_minus_the_in_root_path(
    uri: str, relative: str, folder: str
) -> None:
    metadata = {"evidence_source_uri": uri, "watch_relative_path": relative}

    assert watched_folder(metadata) == folder


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"evidence_source_uri": "file:///data/results/plot.png"},
        {"watch_relative_path": "plot.png"},
        {"evidence_source_uri": "s3://bucket/results/plot.png", "watch_relative_path": "plot.png"},
        {"evidence_source_uri": "file:///data/results/plot.png", "watch_relative_path": "b.png"},
        {"evidence_source_uri": "file:///plot.png", "watch_relative_path": "plot.png"},
    ],
)
def test_watched_folder_refuses_unrelated_or_rootless_paths(metadata: dict) -> None:
    assert watched_folder(metadata) is None


def _observation(
    install_id: str,
    *,
    host: str | None = "rig-7",
    minutes_ago: int = 0,
    version: str | None = None,
    uri: str | None = None,
    relative: str | None = None,
) -> CaptureInstallObservation:
    metadata: dict[str, str] = {"capture_platform": "Darwin"}
    for key, value in (
        ("capture_client_version", version),
        ("capture_client_revision", "a" * 40 if version else None),
        ("evidence_source_uri", uri),
        ("watch_relative_path", relative),
    ):
        if value is not None:
            metadata[key] = value
    return CaptureInstallObservation(
        install_id=install_id,
        host_label=host,
        note_id=uuid4(),
        captured_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        metadata=metadata,
        capture_count=3,
    )


def test_summary_names_a_stale_machine_by_the_folder_it_watches() -> None:
    latest = [_observation(INSTALL_A, version="0.4.2")]
    watched = [
        _observation(
            INSTALL_A, minutes_ago=60, version="0.4.2", uri=FLY_URI, relative="run1/trace.csv"
        )
    ]

    [install] = summarize_capture_installs(latest, watched, server=SERVER)

    assert install.release_status == "behind"
    assert install.client.version == "0.4.2"
    assert install.platform == "Darwin"
    assert install.watched_folder == "fly_walking_data"
    assert install.notice is not None
    assert install.notice.startswith(
        "lab-tracker on the machine watching `fly_walking_data` (rig-7) is behind this "
        "server: it captured with release 0.4.2, and the server runs release 0.5.0."
    )
    assert f"lab-tracker.git@{SERVER.revision}" in install.notice
    assert "`lt update`" in install.notice


@pytest.mark.parametrize(
    ("host", "description"),
    [
        ("rig-7", "lab-tracker on `rig-7` is behind"),
        (None, "lab-tracker on the install `aaaaaaaa`"),
    ],
)
def test_summary_falls_back_to_the_host_then_the_install_id(
    host: str | None, description: str
) -> None:
    [install] = summarize_capture_installs(
        [_observation(INSTALL_A, host=host, version="0.1.0")], [], server=SERVER
    )

    assert install.notice is not None
    assert install.notice.startswith(description)


@pytest.mark.parametrize(
    ("version", "status"),
    [("0.5.0", "current"), ("0.6.0", "ahead"), (None, "unknown"), ("0.5.0rc1", "unknown")],
)
def test_summary_only_writes_a_notice_for_a_machine_behind_the_server(
    version: str | None, status: str
) -> None:
    [install] = summarize_capture_installs(
        [_observation(INSTALL_A, version=version)], [], server=SERVER
    )

    assert install.release_status == status
    assert install.notice is None


def test_summary_lists_the_most_recently_active_machine_first() -> None:
    installs = summarize_capture_installs(
        [
            _observation(INSTALL_A, host="old-rig", minutes_ago=90, version="0.5.0"),
            _observation(INSTALL_B, host="laptop", minutes_ago=5, version="0.5.0"),
        ],
        [],
        server=SERVER,
    )

    assert [install.host_label for install in installs] == ["laptop", "old-rig"]


def _capture(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    text: str,
    metadata: dict[str, str],
) -> str:
    response = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": text, "metadata": metadata},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["note_id"]


def _backdate(client: TestClient, note_id: str, *, days: float) -> None:
    with client.app.state.db_session_factory() as session:
        session.execute(
            update(NoteModel)
            .where(NoteModel.note_id == note_id)
            .values(created_at=datetime.now(timezone.utc) - timedelta(days=days))
        )
        session.commit()


def _host(install_id: str, label: str, version: str) -> dict[str, str]:
    return {
        "capture_install_id": install_id,
        "capture_host_label": label,
        "capture_platform": "Linux",
        "capture_client_version": version,
    }


def _assert_report_names_the_stale_machine(
    client: TestClient,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capture_install_service, "installed_version", lambda: "0.5.0")
    response = client.post("/projects", json={"name": "Capture installs"}, headers=headers)
    assert response.status_code == 201, response.text
    project_id = response.json()["data"]["project_id"]
    watch_metadata = {
        **_host(INSTALL_A, "rig-7", "0.3.0"),
        "evidence_source_uri": FLY_URI,
        "watch_relative_path": "run1/trace.csv",
    }
    watch_note = _capture(client, headers, project_id, "trace.csv", watch_metadata)
    _backdate(client, watch_note, days=1)
    # The newest capture decides the release, even when it is not a watch capture.
    _capture(client, headers, project_id, "figure", _host(INSTALL_A, "rig-7", "0.4.0"))
    _capture(client, headers, project_id, "laptop plot", _host(INSTALL_B, "laptop", "0.5.0"))
    _capture(client, headers, project_id, "typed note", {"source": "manual"})
    stale_note = _capture(client, headers, project_id, "old", _host(INSTALL_C, "old-rig", "0.1"))
    _backdate(client, stale_note, days=200)

    response = client.get(f"/projects/{project_id}/capture-installs", headers=headers)

    assert response.status_code == 200, response.text
    report = response.json()["data"]
    assert report["project_id"] == project_id
    assert report["server"]["version"] == "0.5.0"
    assert report["window_days"] == capture_install_service.CAPTURE_INSTALL_WINDOW_DAYS
    by_host = {install["host_label"]: install for install in report["installs"]}
    assert list(by_host) == ["laptop", "rig-7"]
    rig = by_host["rig-7"]
    assert rig["install_id"] == INSTALL_A
    assert rig["capture_count"] == 2
    assert rig["client"]["version"] == "0.4.0"
    assert rig["release_status"] == "behind"
    assert rig["watched_folder"] == "fly_walking_data"
    assert rig["notice"].startswith(
        "lab-tracker on the machine watching `fly_walking_data` (rig-7) is behind"
    )
    assert by_host["laptop"]["release_status"] == "current"
    assert by_host["laptop"]["notice"] is None


def test_capture_installs_report_joins_each_install_newest_capture(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_report_names_the_stale_machine(client, admin_auth_headers, monkeypatch)


@pytest.mark.postgres
def test_capture_installs_report_on_postgres(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_report_names_the_stale_machine(
        postgres_client, postgres_admin_auth_headers, monkeypatch
    )
