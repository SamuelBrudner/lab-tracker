from __future__ import annotations

from importlib.metadata import version as distribution_version
from pathlib import Path

import pytest

from lab_tracker import __version__ as server_version
from lab_tracker.cli import main as server_main
from lab_tracker_client import __version__ as client_version
from lab_tracker_client.cli import main as client_main
from scripts.verify_release import VersionError, read_project_version, verify_release_tag

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_canonical_version_is_stable_semver_and_matches_runtime_metadata() -> None:
    project_version = read_project_version(REPO_ROOT / "pyproject.toml")

    assert distribution_version("lab-tracker") == project_version
    assert server_version == project_version
    assert client_version == project_version


@pytest.mark.parametrize("main, program", [(server_main, "lab-tracker"), (client_main, "lt")])
def test_cli_version_uses_distribution_version(main, program, capsys) -> None:
    with pytest.raises(SystemExit, match="0"):
        main(["--version"])

    assert capsys.readouterr().out.strip() == f"{program} {distribution_version('lab-tracker')}"


@pytest.mark.parametrize("version", ["1.2", "01.2.3", "1.02.3", "1.2.03", "v1.2.3", "1.2.3rc1"])
def test_project_version_rejects_non_release_semver(tmp_path: Path, version: str) -> None:
    project_file = tmp_path / "pyproject.toml"
    project_file.write_text(
        f'[project]\nname = "example"\nversion = "{version}"\n', encoding="utf-8"
    )

    with pytest.raises(VersionError, match="stable SemVer"):
        read_project_version(project_file)


def test_release_tag_must_exactly_match_version() -> None:
    verify_release_tag("1.2.3", "v1.2.3")

    with pytest.raises(VersionError, match="v1.2.3"):
        verify_release_tag("1.2.3", "v1.2.4")
