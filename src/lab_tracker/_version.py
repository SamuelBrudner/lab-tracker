"""Resolve the installed ``lab-tracker`` distribution version.

``project.version`` in ``pyproject.toml`` is the only editable version source
(see ``docs/versioning.md``); this module reads what that produced at install
time. The ``lab_tracker`` server and ``lab_tracker_client`` packages ship in the
one distribution, so this is the single implementation behind both packages'
``__version__`` and ``lab_tracker.decision_context_constants.package_version``.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

DISTRIBUTION_NAME = "lab-tracker"
# A source tree imported without an install carries no distribution metadata.
UNKNOWN_VERSION = "0.0.0+unknown"


def distribution_version() -> str:
    """Return the installed distribution version, or ``UNKNOWN_VERSION`` when uninstalled."""

    try:
        return version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return UNKNOWN_VERSION


__version__ = distribution_version()
