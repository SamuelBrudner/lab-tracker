"""Which project a client-side capture (a figure save) is filed under.

The order matches the other capture adapters ("repo-local intent wins"):

1. an explicit ``project_id`` argument;
2. ``LAB_TRACKER_PROJECT_ID`` in the environment;
3. the project bound in the saved file's own checkout (its ``lt_ids.json``);
4. the checkout's watch config (``.lab-tracker/watch.json``) project.

Only when none of these names a project does a caller fall back to the
client's or login profile's default project. The first three are *bound*:
a person chose them for this script, shell, or checkout. ``autotrack``, which
fires on every save in every directory, captures only into a bound project,
so a figure is never filed into a default project it was not meant for.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from lab_tracker_client.client import LTValidationError

PROJECT_ENV = "LAB_TRACKER_PROJECT_ID"


class CaptureProjectSource(str, Enum):
    """Where a capture's project came from, strongest first."""

    EXPLICIT = "explicit"
    ENVIRONMENT = "environment"
    CHECKOUT = "checkout"
    WATCH_CONFIG = "watch_config"


# Sources a person chose for this script, shell, or checkout.
BOUND_PROJECT_SOURCES = frozenset(
    {
        CaptureProjectSource.EXPLICIT,
        CaptureProjectSource.ENVIRONMENT,
        CaptureProjectSource.CHECKOUT,
    }
)


class CaptureProject(BaseModel):
    """The project a capture resolved to, and how."""

    model_config = ConfigDict(frozen=True)

    project_id: str
    source: CaptureProjectSource

    @property
    def bound(self) -> bool:
        return self.source in BOUND_PROJECT_SOURCES


def resolve_capture_project(path: Path, *, project_id: str | None) -> CaptureProject | None:
    """Resolve the project for a file saved at ``path``, or ``None`` for a default.

    Never raises for a missing or broken checkout: a file outside any git
    checkout simply has no checkout binding.
    """

    explicit = _optional(project_id)
    if explicit:
        return CaptureProject(project_id=explicit, source=CaptureProjectSource.EXPLICIT)
    from_env = _optional(os.getenv(PROJECT_ENV))
    if from_env:
        return CaptureProject(project_id=from_env, source=CaptureProjectSource.ENVIRONMENT)
    checkout = _checkout_root(path)
    if checkout is None:
        return None
    return _checkout_project(checkout)


def _checkout_project(checkout: Path) -> CaptureProject | None:
    from lab_tracker_client import git_capture

    bound = git_capture.project_from_ids(checkout)
    if bound:
        return CaptureProject(project_id=bound, source=CaptureProjectSource.CHECKOUT)
    config, _config_error = git_capture.resolve_watch_config(checkout)
    configured = _optional(config.project_id)
    if configured:
        return CaptureProject(project_id=configured, source=CaptureProjectSource.WATCH_CONFIG)
    return None


def _checkout_root(path: Path) -> Path | None:
    from lab_tracker_client import git_capture

    try:
        return git_capture.repo_toplevel(path.expanduser().parent)
    except (LTValidationError, OSError):
        return None


def _optional(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = [
    "BOUND_PROJECT_SOURCES",
    "CaptureProject",
    "CaptureProjectSource",
    "resolve_capture_project",
]
