#!/usr/bin/env python3
"""Validate the canonical Lab Tracker version and an optional release tag.

``project.version`` in ``pyproject.toml`` is the only editable version source
(docs/versioning.md). CI runs this without ``--tag`` on every change; the
release workflow adds ``--tag`` so a ``vX.Y.Z`` tag that disagrees with the
declared version fails before anything is built or published.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

PROJECT_FILE_NAME = "pyproject.toml"
# The automated release path accepts stable releases only; pre-release and
# build suffixes need a designed SemVer <-> PEP 440 mapping first.
STABLE_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
TAG_PREFIX = "v"


class VersionError(ValueError):
    """Raised when version metadata does not follow the release contract."""


def read_project_version(project_file: Path) -> str:
    """Return ``project.version`` from ``project_file``, requiring stable SemVer."""

    with project_file.open("rb") as handle:
        project = tomllib.load(handle).get("project", {})
    value = project.get("version")
    if not isinstance(value, str) or not STABLE_SEMVER.fullmatch(value):
        raise VersionError(
            f"{project_file}: project.version must be stable SemVer X.Y.Z; got {value!r}"
        )
    return value


def release_tag_for(version: str) -> str:
    """Return the Git tag that releases ``version``."""

    return f"{TAG_PREFIX}{version}"


def verify_release_tag(version: str, tag: str) -> None:
    """Require ``tag`` to be exactly the release tag for ``version``."""

    expected = release_tag_for(version)
    if tag != expected:
        raise VersionError(f"release tag must be {expected!r}; got {tag!r}")


def _build_parser(repo_root: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-file",
        type=Path,
        default=repo_root / PROJECT_FILE_NAME,
        help=f"Path to the canonical {PROJECT_FILE_NAME}.",
    )
    parser.add_argument(
        "--tag",
        help=f"Require this {TAG_PREFIX}X.Y.Z tag to match project.version.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = _build_parser(repo_root)
    args = parser.parse_args(argv)

    try:
        version = read_project_version(args.project_file)
        if args.tag:
            verify_release_tag(version, args.tag)
    except (OSError, tomllib.TOMLDecodeError, VersionError) as exc:
        parser.error(str(exc))

    print(f"lab-tracker version {version} is valid")
    if args.tag:
        print(f"release tag {args.tag} matches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
