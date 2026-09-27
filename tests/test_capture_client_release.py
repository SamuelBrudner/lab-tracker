"""The coverage read names capture machines whose client is behind the server (GH #238)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lab_tracker import capture_client_release
from lab_tracker.capture_client_release import (
    UPDATE_NOTICE_WINDOW_DAYS,
    client_release,
    server_release,
    watched_folder,
    with_update_notices,
)
from lab_tracker.client_release import ReleaseIdentity
from lab_tracker.config import Settings
from lab_tracker.models import ProjectCoverageCaptureSource

SERVER = ReleaseIdentity(version="0.5.0", revision="b" * 40)
INSTALL_A = "a" * 32
INSTALL_B = "b" * 32
FLY_URI = "file:///Users/sam/data/fly_walking_data/run1/trace.csv"
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


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
def test_watched_folder_refuses_unrelated_or_rootless_paths(metadata: dict[str, str]) -> None:
    assert watched_folder(metadata) is None


def test_client_release_reads_the_capture_metadata_or_stays_unknown() -> None:
    assert client_release(
        {"capture_client_version": "0.4.2", "capture_client_revision": "A" * 40}
    ) == ReleaseIdentity(version="0.4.2", revision="a" * 40)
    assert client_release({"capture_client_revision": "not-a-revision"}) == ReleaseIdentity()


def test_server_release_is_the_installed_version_and_configured_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capture_client_release, "installed_version", lambda: "0.5.0")

    assert server_release(Settings(source_revision="B" * 40)) == SERVER
    assert server_release(Settings(source_revision="unknown")) == ReleaseIdentity(version="0.5.0")


def _source(
    install_id: str | None,
    *,
    host: str | None = "rig-7",
    adapter: str | None = None,
    hours_ago: float = 0,
    version: str | None = None,
    status: str = "unknown",
    folder: str | None = None,
) -> ProjectCoverageCaptureSource:
    return ProjectCoverageCaptureSource(
        evidence_adapter=adapter,
        capture_install_id=install_id,
        capture_host_label=host,
        note_count=3,
        last_capture_at=NOW - timedelta(hours=hours_ago),
        capture_client_version=version,
        capture_client_revision="a" * 40 if version else None,
        release_status=status,
        watched_folder=folder,
    )


def test_notice_goes_on_the_machine_newest_source_and_names_a_watched_folder() -> None:
    figure = _source(INSTALL_A, adapter="lt-figure", version="0.4.2", status="behind")
    watch = _source(
        INSTALL_A,
        adapter="lt-watch",
        hours_ago=1,
        version="0.4.2",
        status="behind",
        folder="fly_walking_data",
    )

    # Input order must not matter: the newest capture decides.
    noticed = with_update_notices([watch, figure], server=SERVER, now=NOW)

    assert [source.evidence_adapter for source in noticed] == ["lt-watch", "lt-figure"]
    assert noticed[0].update_notice is None
    notice = noticed[1].update_notice
    assert notice is not None
    assert notice.startswith(
        "lab-tracker on the machine watching `fly_walking_data` (rig-7) is behind this "
        "server: it captured with release 0.4.2, and the server runs release 0.5.0."
    )
    assert f"lab-tracker.git@{SERVER.revision}" in notice
    assert "`lt update`" in notice


@pytest.mark.parametrize(
    ("host", "description"),
    [
        ("rig-7", "lab-tracker on `rig-7` is behind"),
        (None, "lab-tracker on the install `aaaaaaaa` is behind"),
    ],
)
def test_notice_falls_back_to_the_host_then_the_install_id(
    host: str | None, description: str
) -> None:
    [source] = with_update_notices(
        [_source(INSTALL_A, host=host, version="0.1.0", status="behind")], server=SERVER, now=NOW
    )

    assert source.update_notice is not None
    assert source.update_notice.startswith(description)


@pytest.mark.parametrize("status", ["current", "ahead", "unknown"])
def test_only_a_machine_behind_the_server_gets_a_notice(status: str) -> None:
    [source] = with_update_notices(
        [_source(INSTALL_A, version="0.5.0", status=status)], server=SERVER, now=NOW
    )

    assert source.update_notice is None


def test_a_machine_that_has_updated_since_gets_no_notice() -> None:
    noticed = with_update_notices(
        [
            _source(INSTALL_A, adapter="lt-repo", hours_ago=48, version="0.3.0", status="behind"),
            _source(INSTALL_A, adapter="lt-watch", hours_ago=1, version="0.5.0", status="current"),
        ],
        server=SERVER,
        now=NOW,
    )

    assert [source.update_notice for source in noticed] == [None, None]


def test_a_machine_idle_for_longer_than_the_window_is_not_addressed() -> None:
    idle = _source(
        INSTALL_A, hours_ago=24 * (UPDATE_NOTICE_WINDOW_DAYS + 1), version="0.1.0", status="behind"
    )
    active = _source(
        INSTALL_B,
        host="laptop",
        hours_ago=24 * (UPDATE_NOTICE_WINDOW_DAYS - 1),
        version="0.1.0",
        status="behind",
    )

    noticed = with_update_notices([idle, active], server=SERVER, now=NOW)

    assert noticed[0].update_notice is None
    assert noticed[1].update_notice is not None


def test_sources_without_an_install_id_are_never_addressed() -> None:
    manual = _source(None, host=None, version="0.1.0", status="behind")

    [source] = with_update_notices([manual], server=SERVER, now=NOW)

    assert source == manual
