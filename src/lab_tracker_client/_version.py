"""Resolve the Lab Tracker distribution version at runtime."""

from importlib.metadata import PackageNotFoundError, version


def _distribution_version() -> str:
    try:
        return version("lab-tracker")
    except PackageNotFoundError:
        # Source-only imports can happen before the project is installed.
        return "0.0.0+unknown"


__version__ = _distribution_version()
