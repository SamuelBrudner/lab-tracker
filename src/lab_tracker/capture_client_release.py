"""Which capture machines run a lab-tracker release behind this server.

Every capture carries the capturing client's release next to the host identity
(``capture_client_version`` and ``capture_client_revision``, written by
``lab_tracker_client.client.capture_host_metadata``). The coverage read compares
the release of each capture source's newest capture with this server's release,
so a machine whose client is behind can be named by the folder it watches
instead of only being discovered by someone running a check on that machine.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from lab_tracker.client_release import (
    ReleaseComparison,
    ReleaseIdentity,
    ReleaseStatus,
    installed_version,
    normalized_revision,
    update_steps,
)
from lab_tracker.config import Settings
from lab_tracker.models import NoteMetadataScalar, ProjectCoverageCaptureSource

# Note-metadata keys written by capture clients; the server names them only here.
CAPTURE_CLIENT_VERSION_KEY = "capture_client_version"
CAPTURE_CLIENT_REVISION_KEY = "capture_client_revision"
EVIDENCE_SOURCE_URI_KEY = "evidence_source_uri"
WATCH_RELATIVE_PATH_KEY = "watch_relative_path"
# A machine that has not captured for this long is not addressed: the notice
# is for machines people are still using.
UPDATE_NOTICE_WINDOW_DAYS = 90
FILE_URI_SCHEME = "file"
INSTALL_ID_PREFIX_LENGTH = 8

# One machine is one (install id, host label) pair.
MachineKey = tuple[str, str | None]


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


def release_status(client: ReleaseIdentity, server: ReleaseIdentity) -> ReleaseStatus:
    return ReleaseComparison(client=client, server=server).status


def watched_folder(metadata: Mapping[str, NoteMetadataScalar]) -> str | None:
    """Name the watch root a capture came from: its file URI minus the in-root path."""

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


def with_update_notices(
    sources: Sequence[ProjectCoverageCaptureSource],
    *,
    server: ReleaseIdentity,
    now: datetime,
) -> list[ProjectCoverageCaptureSource]:
    """Write an ``update_notice`` on each stale machine's most recent source.

    A machine's newest capture shows the release its client runs now, so only
    that source can carry the notice; a machine that has updated since an older
    source last captured is not nagged. Any of the machine's watched folders
    may name it. Sources without an install id are manual or unattributed
    captures and are returned unchanged, as is every source in the input order.
    """

    newest, folders = _newest_source_and_folder_by_machine(sources)
    since = now - timedelta(days=UPDATE_NOTICE_WINDOW_DAYS)
    noticed: list[ProjectCoverageCaptureSource] = []
    for source in sources:
        key = _machine_key(source)
        if key is None or newest[key] is not source or not _is_stale(source, since=since):
            noticed.append(source)
            continue
        notice = _update_notice(key, folder=folders.get(key), client=source, server=server)
        noticed.append(source.model_copy(update={"update_notice": notice}))
    return noticed


def _newest_source_and_folder_by_machine(
    sources: Sequence[ProjectCoverageCaptureSource],
) -> tuple[
    dict[MachineKey, ProjectCoverageCaptureSource],
    dict[MachineKey, str],
]:
    newest: dict[MachineKey, ProjectCoverageCaptureSource] = {}
    folders: dict[MachineKey, str] = {}
    for source in sorted(sources, key=lambda item: item.last_capture_at, reverse=True):
        key = _machine_key(source)
        if key is None:
            continue
        newest.setdefault(key, source)
        if source.watched_folder is not None:
            folders.setdefault(key, source.watched_folder)
    return newest, folders


def _machine_key(source: ProjectCoverageCaptureSource) -> MachineKey | None:
    if source.capture_install_id is None:
        return None
    return (source.capture_install_id, source.capture_host_label)


def _is_stale(source: ProjectCoverageCaptureSource, *, since: datetime) -> bool:
    return source.release_status == "behind" and source.last_capture_at >= since


def _update_notice(
    key: MachineKey,
    *,
    folder: str | None,
    client: ProjectCoverageCaptureSource,
    server: ReleaseIdentity,
) -> str:
    return (
        f"lab-tracker on {_machine_description(key, folder)} is behind this server: it "
        f"captured with release {client.capture_client_version}, and the server runs "
        f"release {server.version}. On that machine, {update_steps(server)}."
    )


def _machine_description(key: MachineKey, folder: str | None) -> str:
    install_id, host = key
    if folder:
        return f"the machine watching `{folder}`" + (f" ({host})" if host else "")
    if host:
        return f"`{host}`"
    return f"the install `{install_id[:INSTALL_ID_PREFIX_LENGTH]}`"
