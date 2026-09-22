"""Working-tree (dirty) state must never be claimed clean when git cannot tell.

A slow ``git status`` (large repos, cold caches, Lustre/GPFS/NFS on HPC login
nodes) used to hit a 1-second timeout that was swallowed into
``git_dirty: False`` — a false "clean tree" provenance claim next to a real
commit SHA. These tests drive the repo, hpc and figure adapters with a fake
slow/failing git on ``PATH``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lab_tracker_client import LTValidationError
from lab_tracker_client import figure as figure_module
from lab_tracker_client import hpc as hpc_capture
from lab_tracker_client import repo as repo_capture
from lab_tracker_client.gitinfo import (
    DEFAULT_GIT_TIMEOUT_SECONDS,
    GIT_TIMEOUT_ENV,
    git_timeout_seconds,
)

_REAL_GIT = shutil.which("git")


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def dirty_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    for name in (
        "LAB_TRACKER_REPO_CONFIG",
        "LAB_TRACKER_REPO_OUTBOX",
        "LAB_TRACKER_REPO_RUN_ID",
        "LAB_TRACKER_HPC_CONFIG",
        "LAB_TRACKER_HPC_OUTBOX",
        GIT_TIMEOUT_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    repo = tmp_path / "analysis"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / ".gitignore").write_text(".lab-tracker/\n", encoding="utf-8")
    (repo / "run_analysis.py").write_text("print('v1')\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "run_analysis.py")
    _git(repo, "commit", "-q", "-m", "initial analysis")
    commit = _git(repo, "rev-parse", "HEAD")
    # Uncommitted edit: the working tree is genuinely dirty.
    (repo / "run_analysis.py").write_text("print('v2 uncommitted')\n", encoding="utf-8")
    return repo, commit


def _install_fake_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status_behaviour: str
) -> None:
    """Put a git shim first on PATH whose ``status`` hangs, is slow, or fails."""

    assert _REAL_GIT is not None
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    shim = bin_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = status ]; then\n'
        f"    case {status_behaviour} in\n"
        # exec so a timeout kill hits the sleeping process itself.
        "      hang) exec sleep 30 ;;\n"
        "      slow) sleep 1.5 ;;\n"
        "      fail) echo 'fatal: index file corrupt' >&2; exit 128 ;;\n"
        "    esac\n"
        "  fi\n"
        "done\n"
        f'exec "{_REAL_GIT}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{Path(_REAL_GIT).parent}")


# --- timeout configuration ---------------------------------------------------


def test_git_timeout_defaults_to_documented_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GIT_TIMEOUT_ENV, raising=False)

    assert DEFAULT_GIT_TIMEOUT_SECONDS == 10.0
    assert git_timeout_seconds() == DEFAULT_GIT_TIMEOUT_SECONDS


def test_git_timeout_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GIT_TIMEOUT_ENV, "45.5")

    assert git_timeout_seconds() == 45.5


@pytest.mark.parametrize("raw", ["abc", "0", "-3", "nan", "inf"])
def test_git_timeout_rejects_invalid_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(GIT_TIMEOUT_ENV, raw)

    with pytest.raises(LTValidationError, match=GIT_TIMEOUT_ENV):
        git_timeout_seconds()


# --- repo adapter -----------------------------------------------------------


def test_repo_slow_git_status_within_default_timeout_is_recorded_dirty(
    dirty_repo: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, commit = dirty_repo
    _install_fake_git(tmp_path, monkeypatch, "slow")

    context = repo_capture.git_context(repo)

    assert context["git_commit"] == commit
    assert context["git_dirty"] is True
    assert "git_status_error" not in context


def test_repo_git_status_timeout_records_unknown_not_clean(
    dirty_repo: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, commit = dirty_repo
    _install_fake_git(tmp_path, monkeypatch, "hang")
    monkeypatch.setenv(GIT_TIMEOUT_ENV, "0.5")

    context = repo_capture.git_context(repo)

    assert context["git_commit"] == commit
    assert context["git_dirty"] is None
    assert "timed out after 0.5s" in context["git_status_error"]
    warning = capsys.readouterr().err
    assert "git_dirty" in warning and "unknown" in warning
    assert GIT_TIMEOUT_ENV in warning


def test_repo_git_status_failure_records_unknown_not_clean(
    dirty_repo: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, commit = dirty_repo
    _install_fake_git(tmp_path, monkeypatch, "fail")

    context = repo_capture.git_context(repo)

    assert context["git_commit"] == commit
    assert context["git_dirty"] is None
    assert "index file corrupt" in context["git_status_error"]
    assert "unknown" in capsys.readouterr().err


def test_repo_unknown_dirty_state_is_rendered_and_marked_in_metadata(
    dirty_repo: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, commit = dirty_repo
    monkeypatch.chdir(repo)
    config = repo_capture.init_config(project_id="project-1")
    _install_fake_git(tmp_path, monkeypatch, "hang")
    monkeypatch.setenv(GIT_TIMEOUT_ENV, "0.5")

    event, path, _action = repo_capture.capture_commit(config, event_type="report")
    note = repo_capture.render_event_note(event)
    metadata = repo_capture.event_metadata(
        event,
        source_uri=path.as_uri(),
        source_external_id=repo_capture.event_source_external_id(event),
        content_hash="0" * 64,
    )

    assert event["source"]["git_commit"] == commit
    # Stored events drop null values: the unknown state is "no git_dirty" plus
    # the explicit git_status_error marker, never a False flag.
    stored_source = json.loads(path.read_text(encoding="utf-8"))["source"]
    assert "git_dirty" not in stored_source
    assert "timed out" in stored_source["git_status_error"]
    assert "- Dirty working tree: unknown (git status --porcelain timed out after 0.5s)" in note
    assert "Dirty working tree: False" not in note
    assert "repo_git_dirty" not in metadata
    assert "timed out" in str(metadata["repo_git_status_error"])


def test_repo_known_dirty_state_rendering_is_unchanged(
    dirty_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _commit = dirty_repo
    monkeypatch.chdir(repo)
    config = repo_capture.init_config(project_id="project-1")

    event, path, _action = repo_capture.capture_commit(config, event_type="report")
    metadata = repo_capture.event_metadata(
        event,
        source_uri=path.as_uri(),
        source_external_id=repo_capture.event_source_external_id(event),
        content_hash="0" * 64,
    )

    assert "- Dirty working tree: True\n" in repo_capture.render_event_note(event)
    assert metadata["repo_git_dirty"] is True
    assert "repo_git_status_error" not in metadata


# --- hpc adapter --------------------------------------------------------------


def test_hpc_git_status_timeout_records_unknown_not_clean(
    dirty_repo: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, commit = dirty_repo
    _install_fake_git(tmp_path, monkeypatch, "hang")
    monkeypatch.setenv(GIT_TIMEOUT_ENV, "0.5")
    config = hpc_capture.HpcConfig(
        project_id="project-1", cluster="test-cluster", outbox=str(tmp_path / "outbox")
    )

    event = hpc_capture.make_event(config, event_type="submit", run_id="run-1", cwd=repo)
    note = hpc_capture.render_event_note(event)
    metadata = hpc_capture.event_metadata(
        event,
        source_uri="file:///tmp/event.json",
        source_external_id="hpc:run-1:submit",
        content_hash="0" * 64,
    )

    assert event["source"]["git_commit"] == commit
    assert event["source"].get("git_dirty") is None
    assert "timed out" in event["source"]["git_status_error"]
    assert "- Git dirty: unknown (git status --porcelain timed out after 0.5s)" in note
    assert "hpc_git_dirty" not in metadata
    assert "timed out" in str(metadata["hpc_git_status_error"])
    assert GIT_TIMEOUT_ENV in capsys.readouterr().err


def test_hpc_known_dirty_state_is_unchanged(dirty_repo: tuple[Path, str]) -> None:
    repo, _commit = dirty_repo

    context = hpc_capture.git_context(repo)

    assert context == {"git_commit": context["git_commit"], "git_dirty": True}


# --- figure run_context -------------------------------------------------------


def test_figure_run_context_git_status_timeout_is_not_claimed_clean(
    dirty_repo: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, commit = dirty_repo
    monkeypatch.chdir(repo)
    _install_fake_git(tmp_path, monkeypatch, "hang")
    monkeypatch.setenv(GIT_TIMEOUT_ENV, "0.5")

    with figure_module.run_context() as context:
        metadata = context.to_metadata()

    assert metadata["run_git_commit"] == commit
    assert "run_git_dirty" not in metadata
    assert "timed out" in str(metadata["run_git_status_error"])
    assert GIT_TIMEOUT_ENV in capsys.readouterr().err


def test_figure_run_context_known_dirty_state_is_unchanged(
    dirty_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _commit = dirty_repo
    monkeypatch.chdir(repo)

    with figure_module.run_context() as context:
        metadata = context.to_metadata()

    assert metadata["run_git_dirty"] is True
    assert "run_git_status_error" not in metadata


def test_repo_report_cli_reports_unknown_dirty_state(
    dirty_repo: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from lab_tracker_client import cli as lt_cli

    repo, commit = dirty_repo
    monkeypatch.chdir(repo)
    lt_cli.main(["repo", "init", "--project", "project-1"])
    capsys.readouterr()
    _install_fake_git(tmp_path, monkeypatch, "hang")
    monkeypatch.setenv(GIT_TIMEOUT_ENV, "0.5")

    lt_cli.main(["repo", "report", "--summary", "Pinned analysis state."])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["git_commit"] == commit
    assert payload["git_dirty"] is None
    assert "timed out" in payload["git_status_error"]
    assert GIT_TIMEOUT_ENV in captured.err


def test_hpc_submit_rejects_invalid_git_timeout_before_running_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Validating after sbatch ran would lose the submitted job's record; a bad
    # setting must stop the wrapper before anything is submitted.
    monkeypatch.setenv(GIT_TIMEOUT_ENV, "soon")
    marker = tmp_path / "submitted"
    config = hpc_capture.HpcConfig(
        project_id="project-1", cluster="test-cluster", outbox=str(tmp_path / "outbox")
    )

    with pytest.raises(LTValidationError, match=GIT_TIMEOUT_ENV):
        hpc_capture.run_submit_command(config, ["touch", str(marker)], cwd=tmp_path)

    assert not marker.exists()
