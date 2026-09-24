"""Release-boundary comparison between an installed client and its server.

A release is the ``[project].version`` in ``pyproject.toml``, the value the
dedicated-instance release script already stamps into images. A client is
*behind* only when its server runs a newer release. Exact source revisions are
reported but never nag on their own: most commits are not consumer-relevant,
and a notice that fires on every deploy teaches people to ignore it.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

DISTRIBUTION_NAME = "lab-tracker"
SOURCE_REPOSITORY_URL = "https://github.com/SamuelBrudner/lab-tracker.git"

ReleaseStatus = Literal["current", "behind", "ahead", "unknown"]

_FULL_GIT_REVISION = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")


@dataclass(frozen=True)
class ReleaseIdentity:
    """A release version plus, when known, the exact 40-character revision."""

    version: str | None = None
    revision: str | None = None

    @classmethod
    def from_values(cls, version: object, revision: object) -> ReleaseIdentity:
        cleaned_version = str(version or "").strip() or None
        return cls(version=cleaned_version, revision=normalized_revision(revision))

    def as_dict(self) -> dict[str, str | None]:
        return {"version": self.version, "revision": self.revision}


@dataclass(frozen=True)
class ReleaseComparison:
    client: ReleaseIdentity
    server: ReleaseIdentity

    @property
    def status(self) -> ReleaseStatus:
        client_release = release_key(self.client.version)
        server_release = release_key(self.server.version)
        if client_release is None or server_release is None:
            return "unknown"
        if server_release > client_release:
            return "behind"
        if server_release < client_release:
            return "ahead"
        return "current"

    @property
    def same_revision(self) -> bool | None:
        if self.client.revision is None or self.server.revision is None:
            return None
        return self.client.revision == self.server.revision

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "client_behind_server": self.status == "behind",
            "same_revision": self.same_revision,
            "client": self.client.as_dict(),
            "server": self.server.as_dict(),
        }


def release_key(version: str | None) -> tuple[int, ...] | None:
    """Order dotted-integer releases (semver or date-based); ``None`` otherwise.

    Pre-release, local, and ``0+unknown`` versions deliberately compare as
    unknown, so an unreadable version can never produce an update nag.
    """

    if version is None or _RELEASE_VERSION.fullmatch(version) is None:
        return None
    parts = [int(part) for part in version.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def normalized_revision(value: object) -> str | None:
    revision = str(value or "").strip().lower()
    return revision if _FULL_GIT_REVISION.fullmatch(revision) else None


def installed_version() -> str | None:
    try:
        return importlib.metadata.version(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None


def installed_source_revision() -> str | None:
    """Return the immutable VCS revision recorded by a direct-URL install.

    The guided setup installs Lab Tracker from an exact Git revision. Python
    installers preserve the resolved commit in ``direct_url.json`` (PEP 610),
    which gives both the tool environment and a consumer project's environment
    a local, offline compatibility check.
    """

    with suppress(Exception):
        direct_url_text = importlib.metadata.distribution(DISTRIBUTION_NAME).read_text(
            "direct_url.json"
        )
        if not direct_url_text:
            return None
        vcs_info = json.loads(direct_url_text).get("vcs_info")
        if not isinstance(vcs_info, dict):
            return None
        return normalized_revision(vcs_info.get("commit_id"))
    return None


def installed_release() -> ReleaseIdentity:
    return ReleaseIdentity(version=installed_version(), revision=installed_source_revision())


def release_from_health(payload: object) -> ReleaseIdentity:
    """Read the server release that a ``GET /health`` body reports."""

    app = payload.get("app") if isinstance(payload, dict) else None
    if not isinstance(app, dict):
        return ReleaseIdentity()
    return ReleaseIdentity.from_values(app.get("version"), app.get("source_revision"))


def client_install_command(revision: str | None) -> str | None:
    """The pinned tool install the web Agents page shows for ``revision``."""

    if revision is None:
        return None
    return f'uv tool install --force "lab-tracker @ git+{SOURCE_REPOSITORY_URL}@{revision}"'


def update_steps(server: ReleaseIdentity) -> str:
    """Non-imperative steps that move a client onto ``server``'s release."""

    command = client_install_command(server.revision)
    install = f"`{command}`" if command else "the install command on the server's Agents page"
    return (
        f"{install} installs the server's release, then `lt update` refreshes each "
        "consumer repo and a restarted MCP host picks up the new lt-mcp"
    )
