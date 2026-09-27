"""The one distribution version reaches every place that reports it."""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_metadata_version
from pathlib import Path

import pytest

from lab_tracker import __version__ as server_version
from lab_tracker import _version as version_module
from lab_tracker.cli import main as server_main
from lab_tracker.decision_context_constants import package_version
from lab_tracker_client import __version__ as client_version
from lab_tracker_client.cli import main as client_main
from scripts.verify_release import VersionError, read_project_version, verify_release_tag

REPO_ROOT = Path(__file__).resolve().parent.parent
CliMain = Callable[[list[str] | None], None]


def test_canonical_version_is_stable_semver_and_matches_runtime_metadata() -> None:
    project_version = read_project_version(REPO_ROOT / "pyproject.toml")

    assert distribution_metadata_version(version_module.DISTRIBUTION_NAME) == project_version
    assert server_version == project_version
    assert client_version == project_version


def test_package_version_helper_reports_the_same_version() -> None:
    assert package_version() == server_version
    assert version_module.distribution_version() == server_version


def test_uninstalled_source_tree_reports_the_one_unknown_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_distribution(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(version_module, "version", missing_distribution)

    assert version_module.distribution_version() == version_module.UNKNOWN_VERSION
    assert package_version() == version_module.UNKNOWN_VERSION


@pytest.mark.parametrize(
    ("main", "program"),
    [(server_main, "lab-tracker"), (client_main, "lt")],
    ids=["lab-tracker", "lt"],
)
def test_cli_version_uses_distribution_version(
    main: CliMain, program: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit, match="0"):
        main(["--version"])

    assert capsys.readouterr().out.strip() == f"{program} {server_version}"


@pytest.mark.parametrize("version", ["1.2", "01.2.3", "1.02.3", "1.2.03", "v1.2.3", "1.2.3rc1"])
def test_project_version_rejects_non_release_semver(tmp_path: Path, version: str) -> None:
    project_file = tmp_path / "pyproject.toml"
    project_file.write_text(
        f'[project]\nname = "example"\nversion = "{version}"\n', encoding="utf-8"
    )

    with pytest.raises(VersionError, match="stable SemVer"):
        read_project_version(project_file)


def test_project_version_must_be_declared(tmp_path: Path) -> None:
    project_file = tmp_path / "pyproject.toml"
    project_file.write_text('[project]\nname = "example"\n', encoding="utf-8")

    with pytest.raises(VersionError, match="stable SemVer"):
        read_project_version(project_file)


def test_release_tag_must_exactly_match_version() -> None:
    verify_release_tag("1.2.3", "v1.2.3")

    with pytest.raises(VersionError, match="v1.2.3"):
        verify_release_tag("1.2.3", "v1.2.4")
