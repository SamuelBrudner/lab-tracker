"""Which capture sources were made by a lab-tracker client behind this server.

A capture queued through the watch outbox (``lt watch``, ``lt run``,
``lt pipeline``, coding-agent session, notebook and git capture), by ``lt hpc``,
by the repo hooks (``lt repo report``), or by figure capture carries the
capturing client's release next to the host identity (``capture_client_version``
and ``capture_client_revision``, written by
``lab_tracker_client.client.capture_host_metadata``, which ``watch.make_event``,
``hpc.make_event``, ``repo.make_event`` and figure capture call). A note made by
hand or import (``lt note``, ``lt quick``, ``lt import-folder``, the SDK's
``upsert_note``, ``quick_capture`` and ``upload_note_file``) and the MATLAB
package record no install id or client release themselves, so they produce no
notice, unless a caller writes a ``capture_install_id`` into a note's metadata
by hand. The coverage read compares the release of each capture source's newest
capture with this server's release, so a client that is behind can be named by
what it captures instead of only being discovered by someone running a check on
that machine.

Each source is judged on its own. One install id (``~/.lab-tracker/install-id``)
spans every Python environment on a machine: the ``uv tool`` install that runs
the ``lt`` commands (``lt watch``, ``lt run``, ``lt pipeline``, ``lt hpc``, and
the repo hooks), and each analysis repo's own pinned dependency that saves
figures or notebooks in-script. Those environments run releases of their own, so
neither can speak for the other, and each is updated differently: the tool
environment by reinstalling the server's release, an analysis repo by repinning
its dependency.

A notice is written only when an update is recommended (the client's
(MAJOR, MINOR) is older than the server's, see ``lab_tracker.client_release``
and ``docs/versioning.md``), the source carries an install id, and it captured
within ``UPDATE_NOTICE_WINDOW_DAYS`` (the coverage read's quiet window,
``QUIET_CAPTURE_WINDOW_DAYS``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import Enum
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict

from lab_tracker.client_release import (
    ReleaseComparison,
    ReleaseIdentity,
    ReleaseStatus,
    installed_version,
    normalized_revision,
    project_update_steps,
    release_key,
    update_steps,
)
from lab_tracker.config import Settings
from lab_tracker.models import QUIET_CAPTURE_WINDOW_DAYS, NoteMetadataScalar

# Note-metadata keys written by capture clients; the server names them only here.
CAPTURE_CLIENT_VERSION_KEY = "capture_client_version"
CAPTURE_CLIENT_REVISION_KEY = "capture_client_revision"
CAPTURE_INSTALL_ID_KEY = "capture_install_id"
CAPTURE_HOST_LABEL_KEY = "capture_host_label"
EVIDENCE_ADAPTER_KEY = "evidence_adapter"
EVIDENCE_SOURCE_URI_KEY = "evidence_source_uri"
WATCH_RELATIVE_PATH_KEY = "watch_relative_path"
RUN_REPO_REMOTE_URL_KEY = "run_repo_remote_url"
# Adapters of the `lt watch` family (lt-watch, lt-watch-files, lt-watch-manifest).
WATCH_ADAPTER_PREFIX = "lt-watch"
# Commands of the `lt` CLI, which runs from the `uv tool install` environment.
TOOL_ADAPTER_PREFIX = "lt-"
# `lab_tracker_client` captures made inside an analysis script (savefig).
IN_SCRIPT_ADAPTER_PREFIX = "lab-tracker-client-"
# `run_*` metadata: written in-script by `lab_tracker_client.run_context` and
# also by `lt run`, whose `lt-` adapter is judged first (see capture_environment).
RUN_METADATA_PREFIX = "run_"
# A source that has not captured for this long is not addressed: the notice
# is for clients people are still using. It is the coverage read's quiet
# window, so a source the read calls retired is never nagged to update.
UPDATE_NOTICE_WINDOW_DAYS = QUIET_CAPTURE_WINDOW_DAYS
FILE_URI_SCHEME = "file"
INSTALL_ID_PREFIX_LENGTH = 8


class CaptureEnvironment(str, Enum):
    """Where the capturing client runs, which decides how it is updated."""

    TOOL = "tool"
    ANALYSIS_REPO = "analysis_repo"


class CaptureRelease(BaseModel):
    """What one capture source's newest capture says about its client release."""

    model_config = ConfigDict(frozen=True)

    capture_client_version: str | None
    capture_client_revision: str | None
    release_status: ReleaseStatus
    update_recommended: bool
    watched_folder: str | None
    update_notice: str | None


def server_release(settings: Settings) -> ReleaseIdentity:
    """The release this server runs: its installed version and configured revision."""

    return ReleaseIdentity(
        version=installed_version(),
        revision=normalized_revision(settings.source_revision),
    )


def client_release(metadata: Mapping[str, NoteMetadataScalar]) -> ReleaseIdentity:
    """The release a capture was made with, when its client recorded one."""

    return ReleaseIdentity.from_values(
        metadata.get(CAPTURE_CLIENT_VERSION_KEY),
        metadata.get(CAPTURE_CLIENT_REVISION_KEY),
    )


def predates_release_reporting(
    metadata: Mapping[str, NoteMetadataScalar], server: ReleaseIdentity
) -> bool:
    """An install-stamped capture with no client release, judged against a known server.

    ``capture_host_metadata`` stamps ``capture_client_version`` whenever it
    stamps ``capture_install_id`` (``lab_tracker._version.UNKNOWN_VERSION`` when
    the client cannot read its own release), so an install id alone means the
    client predates release reporting and is behind any server that reads it.
    A note made by hand or import carries no install id of its own, so it is
    not judged unless its metadata was written by hand to include one.
    """

    return (
        bool(metadata.get(CAPTURE_INSTALL_ID_KEY))
        and CAPTURE_CLIENT_VERSION_KEY not in metadata
        and release_key(server.version) is not None
    )


def watched_folder(metadata: Mapping[str, NoteMetadataScalar]) -> str | None:
    """Name the watch root a watch capture came from: its file URI minus the in-root path."""

    adapter = metadata.get(EVIDENCE_ADAPTER_KEY)
    if not isinstance(adapter, str) or not adapter.startswith(WATCH_ADAPTER_PREFIX):
        return None
    uri = metadata.get(EVIDENCE_SOURCE_URI_KEY)
    relative = metadata.get(WATCH_RELATIVE_PATH_KEY)
    if not isinstance(uri, str) or not isinstance(relative, str):
        return None
    parts = urlsplit(uri)
    if parts.scheme != FILE_URI_SCHEME:
        return None
    path = PurePosixPath(unquote(parts.path))
    inside = PurePosixPath(relative).parts
    if not inside or path.parts[-len(inside) :] != inside:
        return None
    return PurePosixPath(*path.parts[: -len(inside)]).name or None


def capture_environment(metadata: Mapping[str, NoteMetadataScalar]) -> CaptureEnvironment:
    """The environment that made a capture: an `lt` command, or an analysis script.

    The adapter is judged first: an ``lt-*`` adapter ran from the tool install,
    ``lt run`` included although it also writes ``run_*`` metadata. Otherwise a
    ``lab-tracker-client-*`` adapter or ``run_*`` metadata marks a capture made
    in-script from an analysis repo's own environment, and any other adapter is
    treated as launched from the tool install. ``lt capture file`` (which the R
    package runs) also writes a
    ``lab-tracker-client-*`` adapter, so it is filed with the in-script captures
    although an ``lt`` executable made it.
    """

    adapter = metadata.get(EVIDENCE_ADAPTER_KEY)
    adapter_name = adapter if isinstance(adapter, str) else ""
    if adapter_name.startswith(TOOL_ADAPTER_PREFIX):
        return CaptureEnvironment.TOOL
    if adapter_name.startswith(IN_SCRIPT_ADAPTER_PREFIX) or any(
        key.startswith(RUN_METADATA_PREFIX) for key in metadata
    ):
        return CaptureEnvironment.ANALYSIS_REPO
    return CaptureEnvironment.TOOL


def capture_release(
    metadata: Mapping[str, NoteMetadataScalar],
    *,
    last_capture_at: datetime,
    server: ReleaseIdentity,
    now: datetime,
) -> CaptureRelease:
    """Judge one capture source by its newest capture's ``metadata``."""

    client = client_release(metadata)
    comparison = ReleaseComparison(client=client, server=server)
    predates = predates_release_reporting(metadata, server)
    status: ReleaseStatus = "behind" if predates else comparison.status
    recommended = predates or comparison.update_recommended
    folder = watched_folder(metadata)
    notice = None
    if _is_addressed(metadata, recommended=recommended, last_capture_at=last_capture_at, now=now):
        notice = _update_notice(metadata, folder=folder, client=client, server=server)
    return CaptureRelease(
        capture_client_version=client.version,
        capture_client_revision=client.revision,
        release_status=status,
        update_recommended=recommended,
        watched_folder=folder,
        update_notice=notice,
    )


def _is_addressed(
    metadata: Mapping[str, NoteMetadataScalar],
    *,
    recommended: bool,
    last_capture_at: datetime,
    now: datetime,
) -> bool:
    # Manual and unattributed captures carry no install id: nobody to address.
    within_window = last_capture_at >= now - timedelta(days=UPDATE_NOTICE_WINDOW_DAYS)
    return recommended and bool(metadata.get(CAPTURE_INSTALL_ID_KEY)) and within_window


def _update_notice(
    metadata: Mapping[str, NoteMetadataScalar],
    *,
    folder: str | None,
    client: ReleaseIdentity,
    server: ReleaseIdentity,
) -> str:
    if capture_environment(metadata) is CaptureEnvironment.ANALYSIS_REPO:
        subject = f"lab-tracker in {_analysis_repo_description(metadata)}"
        steps = f"In that analysis repo, {project_update_steps(server)}."
    else:
        subject = f"lab-tracker on {_machine_description(metadata, folder)}"
        steps = f"On that machine, {update_steps(server)}."
    return f"{subject} {_release_gap(client, server)} {steps}"


def _release_gap(client: ReleaseIdentity, server: ReleaseIdentity) -> str:
    if client.version is None:
        return (
            "predates release reporting, so it is behind this server, which runs "
            f"release {server.version}."
        )
    return (
        f"is behind this server: it captured with release {client.version}, and the "
        f"server runs release {server.version}."
    )


def _analysis_repo_description(metadata: Mapping[str, NoteMetadataScalar]) -> str:
    machine = _host_description(metadata)
    repo = metadata.get(RUN_REPO_REMOTE_URL_KEY)
    if isinstance(repo, str) and repo:
        return f"the analysis repo `{repo}` on {machine}"
    return f"an analysis-repo environment on {machine}"


def _machine_description(metadata: Mapping[str, NoteMetadataScalar], folder: str | None) -> str:
    host = metadata.get(CAPTURE_HOST_LABEL_KEY)
    if folder:
        return f"the machine watching `{folder}`" + (f" ({host})" if host else "")
    return _host_description(metadata)


def _host_description(metadata: Mapping[str, NoteMetadataScalar]) -> str:
    host = metadata.get(CAPTURE_HOST_LABEL_KEY)
    if host:
        return f"`{host}`"
    install_id = str(metadata.get(CAPTURE_INSTALL_ID_KEY))
    return f"the install `{install_id[:INSTALL_ID_PREFIX_LENGTH]}`"
