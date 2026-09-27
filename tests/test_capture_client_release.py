"""The coverage read names capture machines whose client is behind the server (GH #238)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lab_tracker import capture_client_release
from lab_tracker._version import UNKNOWN_VERSION
from lab_tracker.capture_client_release import (
    UPDATE_NOTICE_WINDOW_DAYS,
    CaptureRelease,
    capture_release,
    client_release,
    server_release,
    watched_folder,
)
from lab_tracker.client_release import ReleaseIdentity
from lab_tracker.config import Settings
from lab_tracker.models import NoteMetadataScalar

SERVER = ReleaseIdentity(version="0.5.0", revision="b" * 40)
INSTALL_A = "a" * 32
FIGURE_ADAPTER = "lab-tracker-client-figure"
REPO_URL = "git@github.com:lab/fly-analysis.git"
FLY_URI = "file:///Users/sam/data/fly_walking_data/run1/trace.csv"
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("adapter", ["lt-watch", "lt-watch-files", "lt-watch-manifest"])
@pytest.mark.parametrize(
    ("uri", "relative", "folder"),
    [
        (FLY_URI, "run1/trace.csv", "fly_walking_data"),
        ("file:///C:/lab/fly%20walking/run1/trace.csv", "run1/trace.csv", "fly walking"),
        ("file:///data/results/plot.png", "plot.png", "results"),
    ],
)
def test_watched_folder_is_the_file_uri_minus_the_in_root_path(
    adapter: str, uri: str, relative: str, folder: str
) -> None:
    metadata = {
        "evidence_adapter": adapter,
        "evidence_source_uri": uri,
        "watch_relative_path": relative,
    }

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
    assert watched_folder({"evidence_adapter": "lt-watch", **metadata}) is None


@pytest.mark.parametrize("adapter", [None, FIGURE_ADAPTER, "lt-hpc", "lt-import-folder"])
def test_only_a_watch_adapter_names_a_watched_folder(adapter: str | None) -> None:
    metadata = {"evidence_source_uri": FLY_URI, "watch_relative_path": "run1/trace.csv"}
    if adapter is not None:
        metadata["evidence_adapter"] = adapter

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


def _metadata(
    adapter: str | None,
    *,
    install_id: str | None = INSTALL_A,
    host: str | None = "rig-7",
    version: str | None = None,
    **extra: NoteMetadataScalar,
) -> dict[str, NoteMetadataScalar]:
    metadata: dict[str, NoteMetadataScalar] = dict(extra)
    if adapter is not None:
        metadata["evidence_adapter"] = adapter
    if install_id is not None:
        metadata["capture_install_id"] = install_id
    if host is not None:
        metadata["capture_host_label"] = host
    if version is not None:
        metadata["capture_client_version"] = version
        metadata["capture_client_revision"] = "a" * 40
    return metadata


def _watch(version: str | None, **extra: NoteMetadataScalar) -> dict[str, NoteMetadataScalar]:
    return _metadata(
        "lt-watch",
        version=version,
        evidence_source_uri=FLY_URI,
        watch_relative_path="run1/trace.csv",
        **extra,
    )


def _judge(
    metadata: dict[str, NoteMetadataScalar],
    *,
    hours_ago: float = 0,
    server: ReleaseIdentity = SERVER,
) -> CaptureRelease:
    return capture_release(
        metadata, last_capture_at=NOW - timedelta(hours=hours_ago), server=server, now=NOW
    )


def test_a_stale_watch_source_names_its_folder_and_the_tool_install() -> None:
    judged = _judge(_watch("0.4.2"))

    assert judged.capture_client_version == "0.4.2"
    assert judged.release_status == "behind"
    assert judged.update_recommended is True
    assert judged.watched_folder == "fly_walking_data"
    notice = judged.update_notice
    assert notice is not None
    assert notice.startswith(
        "lab-tracker on the machine watching `fly_walking_data` (rig-7) is behind this "
        "server: it captured with release 0.4.2, and the server runs release 0.5.0."
    )
    assert 'uv tool install --force "lab-tracker @ git+' in notice
    assert f"lab-tracker.git@{SERVER.revision}" in notice
    assert "`lt update`" in notice


def test_a_stale_figure_environment_is_told_to_repin_its_analysis_repo() -> None:
    judged = _judge(
        _metadata(
            FIGURE_ADAPTER,
            version="0.4.0",
            # A figure never borrows a folder: it is not a watch capture.
            evidence_source_uri=FLY_URI,
            watch_relative_path="run1/trace.csv",
        )
    )

    assert judged.watched_folder is None
    notice = judged.update_notice
    assert notice is not None
    assert notice.startswith(
        "lab-tracker in an analysis-repo environment on `rig-7` is behind this server: it "
        "captured with release 0.4.0, and the server runs release 0.5.0."
    )
    assert '`uv add "lab-tracker @ git+' in notice
    assert f"lab-tracker.git@{SERVER.revision}" in notice
    assert "uv tool install" not in notice
    assert "fly_walking_data" not in notice


def test_an_in_script_capture_is_recognised_by_its_run_metadata_and_names_the_repo() -> None:
    judged = _judge(
        _metadata(
            "my-analysis-export",
            version="0.4.0",
            run_captured_at="2026-09-27T11:00:00Z",
            run_repo_remote_url=REPO_URL,
        )
    )

    notice = judged.update_notice
    assert notice is not None
    assert notice.startswith(
        f"lab-tracker in the analysis repo `{REPO_URL}` on `rig-7` is behind this server"
    )
    assert "uv add" in notice
    assert "uv tool install" not in notice


@pytest.mark.parametrize(
    "adapter", ["lt-hpc", "lt-repo", "lt-import-folder", "lt-watch-files", "custom-importer"]
)
def test_tool_environment_captures_are_told_to_reinstall_the_tool(adapter: str) -> None:
    notice = _judge(_metadata(adapter, version="0.4.0")).update_notice

    assert notice is not None
    assert notice.startswith("lab-tracker on `rig-7` is behind this server")
    assert "uv tool install" in notice
    assert "uv add" not in notice


def test_every_stale_source_on_a_machine_is_judged_on_its_own() -> None:
    # One install id spans the uv tool env and each analysis-repo venv, so a
    # current watcher capturing last must not hide a stale figure environment.
    stale_figure = _judge(_metadata(FIGURE_ADAPTER, version="0.4.0"), hours_ago=2)
    current_watch = _judge(_watch("0.5.0"), hours_ago=1)
    stale_watch = _judge(_watch("0.3.0"), hours_ago=2)
    current_figure = _judge(_metadata(FIGURE_ADAPTER, version="0.5.0"), hours_ago=1)

    assert stale_figure.update_notice is not None
    assert current_watch.update_notice is None
    assert stale_watch.update_notice is not None
    assert "`fly_walking_data`" in stale_watch.update_notice
    assert current_figure.update_notice is None


def test_a_patch_release_gap_is_reported_but_never_nags() -> None:
    judged = _judge(_watch("0.5.0"), server=ReleaseIdentity(version="0.5.3", revision="b" * 40))

    assert judged.release_status == "behind"
    assert judged.update_recommended is False
    assert judged.update_notice is None


@pytest.mark.parametrize(
    ("version", "status"),
    [("0.5.0", "current"), ("0.6.0", "ahead"), ("0.5.0rc1", "unknown")],
)
def test_only_a_source_behind_the_server_gets_a_notice(version: str, status: str) -> None:
    judged = _judge(_watch(version))

    assert judged.release_status == status
    assert judged.update_recommended is False
    assert judged.update_notice is None


def test_a_client_that_predates_release_reporting_is_behind() -> None:
    judged = _judge(_watch(None))

    assert judged.capture_client_version is None
    assert judged.release_status == "behind"
    assert judged.update_recommended is True
    notice = judged.update_notice
    assert notice is not None
    assert notice.startswith(
        "lab-tracker on the machine watching `fly_walking_data` (rig-7) predates release "
        "reporting, so it is behind this server, which runs release 0.5.0."
    )
    assert "uv tool install" in notice


def test_a_predating_client_is_unknown_while_the_server_release_is_unknown() -> None:
    judged = _judge(_watch(None), server=ReleaseIdentity(revision="b" * 40))

    assert judged.release_status == "unknown"
    assert judged.update_notice is None


def test_a_client_that_cannot_read_its_own_release_is_unknown_not_predating() -> None:
    judged = _judge(_metadata(FIGURE_ADAPTER, capture_client_version=UNKNOWN_VERSION))

    assert judged.release_status == "unknown"
    assert judged.update_recommended is False
    assert judged.update_notice is None


def test_captures_without_an_install_id_are_never_addressed() -> None:
    judged = _judge(_metadata(None, install_id=None, host=None))

    assert judged.release_status == "unknown"
    assert judged.update_notice is None


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
    notice = _judge(_metadata("lt-hpc", host=host, version="0.1.0")).update_notice

    assert notice is not None
    assert notice.startswith(description)


def test_a_source_idle_for_longer_than_the_window_is_not_addressed() -> None:
    idle = _judge(_watch("0.1.0"), hours_ago=24 * (UPDATE_NOTICE_WINDOW_DAYS + 1))
    active = _judge(_watch("0.1.0"), hours_ago=24 * (UPDATE_NOTICE_WINDOW_DAYS - 1))

    assert idle.release_status == "behind"
    assert idle.update_notice is None
    assert active.update_notice is not None


def test_the_update_notice_window_is_the_quiet_capture_window() -> None:
    """One idea of an active source: a machine the coverage read calls
    retired (silent past the quiet window) is never nagged to update."""

    from lab_tracker.models import QUIET_CAPTURE_WINDOW_DAYS

    assert UPDATE_NOTICE_WINDOW_DAYS == QUIET_CAPTURE_WINDOW_DAYS
    retired = _judge(_watch("0.1.0"), hours_ago=24 * (QUIET_CAPTURE_WINDOW_DAYS + 1))
    assert retired.release_status == "behind"
    assert retired.update_notice is None
