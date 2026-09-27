"""Release comparison between an installed client and its server.

A release is the ``[project].version`` in ``pyproject.toml``, versioned by
``docs/versioning.md``: Semantic Versioning, and while on ``0.y.z`` a MINOR
bump for features and any incompatibility and a PATCH bump only for
backward-compatible fixes. ``status`` is truthful: a client on any older
release is *behind*. One rule decides whether that is worth a nag:
``update_recommended`` is true only when the client's (MAJOR, MINOR) is older
than the server's, and every update notice (the coverage read's
``update_notice``, the ``lt-mcp`` notice, the ``lt setup status`` suggestion)
keys on it. A PATCH-only gap is reported as information, never suggested.
Exact source revisions are reported but never nag on their own either: most
commits are not consumer-relevant, and a notice that fires on every deploy
teaches people to ignore it.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from lab_tracker import _version

SOURCE_REPOSITORY_URL = "https://github.com/SamuelBrudner/lab-tracker.git"
# (MAJOR, MINOR): the part of a release that carries features and, on 0.y.z,
# incompatibilities (docs/versioning.md).
FEATURE_LINE_LENGTH = 2
# Where a person finds the pinned project dependency when no revision is known
# (setup guide step 5, "Project Python dependency").
SETUP_PAGE_PROJECT_INSTALL = "the pinned `uv add` command on the server's Setup page"

ReleaseStatus = Literal["current", "behind", "ahead", "unknown"]
FeatureLine = tuple[int, ...]

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
    def update_recommended(self) -> bool:
        return recommends_update(self.client.version, self.server.version)

    @property
    def same_revision(self) -> bool | None:
        if self.client.revision is None or self.server.revision is None:
            return None
        return self.client.revision == self.server.revision

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "client_behind_server": self.status == "behind",
            "update_recommended": self.update_recommended,
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


def feature_line(version: str | None) -> FeatureLine | None:
    """The (MAJOR, MINOR) of a plain dotted release; ``None`` when unreadable."""

    if version is None or release_key(version) is None:
        return None
    parts = [int(part) for part in version.split(".")]
    parts.extend([0] * FEATURE_LINE_LENGTH)
    return tuple(parts[:FEATURE_LINE_LENGTH])


def recommends_update(client_version: str | None, server_version: str | None) -> bool:
    """True only when the client's (MAJOR, MINOR) is older than the server's."""

    client_line = feature_line(client_version)
    server_line = feature_line(server_version)
    if client_line is None or server_line is None:
        return False
    return client_line < server_line


def normalized_revision(value: object) -> str | None:
    revision = str(value or "").strip().lower()
    return revision if _FULL_GIT_REVISION.fullmatch(revision) else None


def installed_version() -> str | None:
    """The installed distribution version, or ``None`` for an uninstalled source tree."""

    version = _version.distribution_version()
    return None if version == _version.UNKNOWN_VERSION else version


def installed_source_revision() -> str | None:
    """Return the immutable VCS revision recorded by a direct-URL install.

    The guided setup installs Lab Tracker from an exact Git revision. Python
    installers preserve the resolved commit in ``direct_url.json`` (PEP 610),
    which gives both the tool environment and a consumer project's environment
    a local, offline compatibility check.
    """

    with suppress(Exception):
        direct_url_text = importlib.metadata.distribution(_version.DISTRIBUTION_NAME).read_text(
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


def project_install_command(revision: str | None) -> str | None:
    """The pinned project dependency the Setup page shows for ``revision``."""

    if revision is None:
        return None
    return f'uv add "lab-tracker @ git+{SOURCE_REPOSITORY_URL}@{revision}"'


def update_steps(server: ReleaseIdentity) -> str:
    """Non-imperative steps that move the ``uv tool`` install onto ``server``'s release."""

    command = client_install_command(server.revision)
    install = f"`{command}`" if command else "the install command on the server's Agents page"
    return (
        f"{install} installs the server's release, then `lt update` refreshes each "
        "consumer repo and a restarted MCP host picks up the new lt-mcp"
    )


def project_update_steps(server: ReleaseIdentity) -> str:
    """Non-imperative steps that repin an analysis repo's dependency to ``server``'s release."""

    command = project_install_command(server.revision)
    install = f"`{command}`" if command else SETUP_PAGE_PROJECT_INSTALL
    return (
        f"{install} updates that repo's pinned lab-tracker dependency to the server's "
        "release; `lt update` does not change that pin"
    )
