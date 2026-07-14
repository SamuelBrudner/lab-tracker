#!/usr/bin/env python3
"""Validate the canonical Lab Tracker version and an optional release tag."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


STABLE_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class VersionError(ValueError):
    """Raised when version metadata does not follow the release contract."""


def read_project_version(project_file: Path) -> str:
    with project_file.open("rb") as handle:
        project = tomllib.load(handle).get("project", {})
    value = project.get("version")
    if not isinstance(value, str) or not STABLE_SEMVER.fullmatch(value):
        raise VersionError(
            f"{project_file}: project.version must be stable SemVer X.Y.Z; got {value!r}"
        )
    return value


def verify_release_tag(version: str, tag: str) -> None:
    expected = f"v{version}"
    if tag != expected:
        raise VersionError(f"release tag must be {expected!r}; got {tag!r}")


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-file",
        type=Path,
        default=repo_root / "pyproject.toml",
        help="Path to the canonical pyproject.toml.",
    )
    parser.add_argument("--tag", help="Require this vX.Y.Z tag to match project.version.")
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
