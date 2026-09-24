"""Name the capture machines whose Lab Tracker client is behind this server.

Every capture carries the capturing install's id, host label, and client
release (``capture_host_metadata``); this joins each install's newest capture
in a project against the server's own release, so a stale machine can be
named by what it watches rather than only discovered locally on that machine.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Protocol
from urllib.parse import unquote, urlsplit
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.client_release import (
    ReleaseComparison,
    ReleaseIdentity,
    installed_version,
    normalized_revision,
    update_steps,
)
from lab_tracker.models import (
    CaptureInstall,
    CaptureInstallObservation,
    CaptureInstallRelease,
    CaptureInstallReport,
    NoteMetadataScalar,
    Project,
)
from lab_tracker.services.base import BaseService, ServiceContext

# A machine that has not captured into the project for this long is not
# addressed: the notice is for machines people are still using.
CAPTURE_INSTALL_WINDOW_DAYS = 90


class ProjectReadAccess(Protocol):
    def get_project_for_read(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Project: ...


class CaptureInstallService(BaseService):
    def __init__(self, context: ServiceContext, *, projects: ProjectReadAccess) -> None:
        super().__init__(context)
        self.projects = projects

    def report(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> CaptureInstallReport:
        self.projects.get_project_for_read(project_id, actor=actor)
        since = datetime.now(timezone.utc) - timedelta(days=CAPTURE_INSTALL_WINDOW_DAYS)
        repository = self.repository
        latest = repository.latest_capture_install_notes(project_id=project_id, since=since)
        watched = repository.latest_capture_install_notes(
            project_id=project_id,
            since=since,
            watch_only=True,
        )
        server = ReleaseIdentity(
            version=installed_version(),
            revision=normalized_revision(self._context.active_settings().source_revision),
        )
        return CaptureInstallReport(
            project_id=project_id,
            server=CaptureInstallRelease(**server.as_dict()),
            window_days=CAPTURE_INSTALL_WINDOW_DAYS,
            installs=summarize_capture_installs(latest, watched, server=server),
        )


def summarize_capture_installs(
    latest: list[CaptureInstallObservation],
    watched: list[CaptureInstallObservation],
    *,
    server: ReleaseIdentity,
) -> list[CaptureInstall]:
    """One entry per install, newest first; a watch capture names its folder."""

    folders = {
        (observation.install_id, observation.host_label): watched_folder(observation.metadata)
        for observation in watched
    }
    installs = [
        _capture_install(
            observation,
            folder=folders.get((observation.install_id, observation.host_label)),
            server=server,
        )
        for observation in latest
    ]
    return sorted(installs, key=lambda install: install.last_captured_at, reverse=True)


def _capture_install(
    observation: CaptureInstallObservation,
    *,
    folder: str | None,
    server: ReleaseIdentity,
) -> CaptureInstall:
    metadata = observation.metadata
    client = ReleaseIdentity.from_values(
        metadata.get("capture_client_version"),
        metadata.get("capture_client_revision"),
    )
    comparison = ReleaseComparison(client=client, server=server)
    notice = None
    if comparison.status == "behind":
        where = _machine_description(observation, folder)
        notice = (
            f"lab-tracker on {where} is behind this server: it captured with release "
            f"{client.version}, and the server runs release {server.version}. On that "
            f"machine, {update_steps(server)}."
        )
    platform = metadata.get("capture_platform")
    return CaptureInstall(
        install_id=observation.install_id,
        host_label=observation.host_label,
        platform=str(platform) if platform else None,
        client=CaptureInstallRelease(**client.as_dict()),
        release_status=comparison.status,
        last_captured_at=observation.captured_at,
        last_note_id=observation.note_id,
        capture_count=observation.capture_count,
        watched_folder=folder,
        notice=notice,
    )


def _machine_description(observation: CaptureInstallObservation, folder: str | None) -> str:
    host = observation.host_label
    if folder:
        return f"the machine watching `{folder}`" + (f" ({host})" if host else "")
    if host:
        return f"`{host}`"
    return f"the install `{observation.install_id[:8]}`"


def watched_folder(metadata: Mapping[str, NoteMetadataScalar]) -> str | None:
    """Name the watch root a capture came from: its file URI minus the in-root path."""

    uri = metadata.get("evidence_source_uri")
    relative = metadata.get("watch_relative_path")
    if not isinstance(uri, str) or not isinstance(relative, str):
        return None
    parts = urlsplit(uri)
    if parts.scheme != "file":
        return None
    path = PurePosixPath(unquote(parts.path))
    inside = PurePosixPath(relative).parts
    if not inside or path.parts[-len(inside) :] != inside:
        return None
    return PurePosixPath(*path.parts[: -len(inside)]).name or None
