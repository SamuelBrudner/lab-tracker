"""The one distribution version reaches every place that reports it."""

from __future__ import annotations

import re
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
from scripts.verify_release import (
    VersionError,
    read_project_version,
    verify_release_tag,
)
from scripts.verify_release import main as verify_release_main

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
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


def _project_file(tmp_path: Path, version: str) -> Path:
    project_file = tmp_path / "pyproject.toml"
    project_file.write_text(
        f'[project]\nname = "example"\nversion = "{version}"\n', encoding="utf-8"
    )
    return project_file


def test_print_tag_prints_only_the_release_tag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_file = _project_file(tmp_path, "1.2.3")

    assert verify_release_main(["--project-file", str(project_file), "--print-tag"]) == 0

    assert capsys.readouterr().out == "v1.2.3\n"


def test_print_tag_refuses_a_non_release_version(tmp_path: Path) -> None:
    project_file = _project_file(tmp_path, "1.2.3rc1")

    with pytest.raises(SystemExit, match="2"):
        verify_release_main(["--project-file", str(project_file), "--print-tag"])


def test_only_a_green_ci_run_on_main_starts_an_automatic_release() -> None:
    # Renaming the ci workflow, or loosening these conditions, would silently
    # stop releases or release an untested commit.
    ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    auto = (WORKFLOWS / "auto-release.yml").read_text(encoding="utf-8")
    ci_name = re.search(r"^name: (\S+)$", ci, re.MULTILINE)

    assert ci_name is not None
    assert f"workflows: [{ci_name[1]}]" in auto
    assert "branches: [main]" in auto
    assert "github.event.workflow_run.conclusion == 'success'" in auto
    assert "github.event.workflow_run.event == 'push'" in auto
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in auto
    assert "uses: ./.github/workflows/release.yml" in auto
    assert "sha: ${{ github.event.workflow_run.head_sha }}" in auto


def test_release_workflow_tags_a_called_release_only_after_its_checks() -> None:
    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    # A called run's ref is main, never the tag, so nothing may read GITHUB_REF_NAME.
    assert "GITHUB_REF_NAME" not in release
    steps = [
        'scripts/verify_release.py --tag "$RELEASE_TAG"',
        "pytest -q",
        "uv build --no-sources",
        'git push origin "refs/tags/$RELEASE_TAG"',
        'gh release create "$RELEASE_TAG"',
    ]
    positions = [release.find(step) for step in steps]

    assert -1 not in positions, dict(zip(steps, positions, strict=True))
    assert positions == sorted(positions)
