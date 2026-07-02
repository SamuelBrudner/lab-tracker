from __future__ import annotations

import json
import subprocess
import sys

import pytest

from lab_tracker_client import cli as lt_cli
from lab_tracker_client.client import LTValidationError
from lab_tracker_client.repo import (
    HOOK_BEGIN_MARKER,
    HOOK_END_MARKER,
    install_post_commit_hook,
)


def _clear_repo_env(monkeypatch) -> None:
    monkeypatch.delenv("LAB_TRACKER_REPO_CONFIG", raising=False)
    monkeypatch.delenv("LAB_TRACKER_REPO_OUTBOX", raising=False)
    monkeypatch.delenv("LAB_TRACKER_REPO_RUN_ID", raising=False)
    monkeypatch.delenv("LAB_TRACKER_REPO_HOOK_ENABLED", raising=False)
    monkeypatch.delenv("LAB_TRACKER_LT", raising=False)


def _git(path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_git_repo(path) -> str:
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "remote", "add", "origin", "https://example.com/org/repo.git")
    (path / ".gitignore").write_text(".lab-tracker/\n", encoding="utf-8")
    (path / "analysis.py").write_text("print('hi')\n", encoding="utf-8")
    _git(path, "add", ".gitignore", "analysis.py")
    _git(path, "commit", "-q", "-m", "initial analysis")
    return _git(path, "rev-parse", "HEAD")


def test_repo_cli_init_report_status(tmp_path, monkeypatch, capsys) -> None:
    _clear_repo_env(monkeypatch)
    commit = _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    lt_cli.main(["repo", "init", "--project", "project-1"])
    capsys.readouterr()

    lt_cli.main(["repo", "report", "--summary", "Pinned analysis state."])
    report_payload = json.loads(capsys.readouterr().out)
    lt_cli.main(["repo", "status"])
    status_payload = json.loads(capsys.readouterr().out)

    assert report_payload["command"] == "repo-report"
    assert report_payload["git_commit"] == commit
    assert report_payload["git_dirty"] is False
    assert status_payload["command"] == "repo-status"
    assert status_payload["pending"] == 1
    event_files = list((tmp_path / ".lab-tracker" / "outbox" / "repo").glob("*.json"))
    assert len(event_files) == 1


def test_repo_cli_report_fail_silent_without_config(tmp_path, monkeypatch, capsys) -> None:
    _clear_repo_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # no config, no git repo

    lt_cli.main(["repo", "report", "--fail-silent"])  # must not raise

    assert capsys.readouterr().out == ""


def test_repo_cli_report_without_config_raises(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(LTValidationError):
        lt_cli.main(["repo", "report"])


# --- hook installer ---------------------------------------------------------


def test_install_hook_creates_managed_block(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)

    result = install_post_commit_hook(tmp_path, lt_command="/opt/venv/bin/lt")

    hook = tmp_path / ".git" / "hooks" / "post-commit"
    content = hook.read_text(encoding="utf-8")
    assert result["action"] == "installed"
    assert content.startswith("#!/usr/bin/env sh")
    assert HOOK_BEGIN_MARKER in content
    assert HOOK_END_MARKER in content
    assert "/opt/venv/bin/lt" in content
    assert "repo report --fail-silent" in content
    assert hook.stat().st_mode & 0o111  # executable


def test_install_hook_is_idempotent_and_updates_in_place(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)

    install_post_commit_hook(tmp_path, lt_command="/old/lt")
    result = install_post_commit_hook(tmp_path, lt_command="/new/lt")

    content = (tmp_path / ".git" / "hooks" / "post-commit").read_text(encoding="utf-8")
    assert result["action"] == "updated"
    assert "/new/lt" in content
    assert "/old/lt" not in content
    assert content.count(HOOK_BEGIN_MARKER) == 1


def test_install_hook_refuses_foreign_hook_without_force(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho custom hook\n", encoding="utf-8")

    with pytest.raises(LTValidationError):
        install_post_commit_hook(tmp_path, lt_command="lt")

    appended = install_post_commit_hook(tmp_path, lt_command="lt", force=True)
    content = hook.read_text(encoding="utf-8")
    assert appended["action"] == "appended"
    assert "echo custom hook" in content  # foreign hook preserved
    assert HOOK_BEGIN_MARKER in content


def test_install_hook_outside_git_repo_raises(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)

    with pytest.raises(LTValidationError):
        install_post_commit_hook(tmp_path)


def test_install_hook_converts_windows_paths_for_sh(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)

    install_post_commit_hook(tmp_path, lt_command="C:\\venv\\Scripts\\lt.exe")

    content = (tmp_path / ".git" / "hooks" / "post-commit").read_text(encoding="utf-8")
    assert "C:/venv/Scripts/lt.exe" in content
    assert "\\" not in content.split(HOOK_BEGIN_MARKER, 1)[1]


@pytest.mark.skipif(sys.platform == "win32", reason="sh hook execution test is POSIX-only")
def test_installed_hook_records_commit_end_to_end(tmp_path, monkeypatch) -> None:
    """A real `git commit` fires the hook, which writes an outbox event."""

    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    lt_cli.main(["repo", "init", "--project", "project-1"])
    # Call this checkout's CLI directly: `lt` may not be on the hook's PATH.
    lt_shim = tmp_path / "lt-shim"
    lt_shim.write_text(
        "#!/usr/bin/env sh\n"
        f'exec "{sys.executable}" -m lab_tracker_client "$@"\n',
        encoding="utf-8",
    )
    lt_shim.chmod(0o755)
    install_post_commit_hook(tmp_path, lt_command=str(lt_shim))

    (tmp_path / "analysis.py").write_text("print('v2')\n", encoding="utf-8")
    _git(tmp_path, "add", "analysis.py")
    _git(tmp_path, "commit", "-q", "-m", "second analysis")
    commit = _git(tmp_path, "rev-parse", "HEAD")

    outbox = tmp_path / ".lab-tracker" / "outbox" / "repo"
    events = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(outbox.glob("*.json"))]
    assert any(e["source"]["git_commit"] == commit for e in events)


@pytest.mark.skipif(sys.platform == "win32", reason="sh hook execution test is POSIX-only")
def test_hook_never_blocks_commit_when_lt_fails(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    # No `lt repo init` -> report fails; the commit must still succeed.
    broken_lt = tmp_path / "broken-lt"
    broken_lt.write_text("#!/usr/bin/env sh\nexit 1\n", encoding="utf-8")
    broken_lt.chmod(0o755)
    install_post_commit_hook(tmp_path, lt_command=str(broken_lt))

    (tmp_path / "analysis.py").write_text("print('v3')\n", encoding="utf-8")
    _git(tmp_path, "add", "analysis.py")
    _git(tmp_path, "commit", "-q", "-m", "commit survives broken hook")

    assert _git(tmp_path, "log", "-1", "--pretty=%s") == "commit survives broken hook"
