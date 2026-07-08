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
    _hook_command_path,
    install_post_commit_hook,
)


def _clear_repo_env(monkeypatch) -> None:
    monkeypatch.delenv("LAB_TRACKER_REPO_CONFIG", raising=False)
    monkeypatch.delenv("LAB_TRACKER_REPO_OUTBOX", raising=False)
    monkeypatch.delenv("LAB_TRACKER_REPO_RUN_ID", raising=False)
    monkeypatch.delenv("LAB_TRACKER_CONTAINER_REF", raising=False)
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


def _init_repo_config(path) -> None:
    from lab_tracker_client.repo import init_config

    init_config(
        project_id="project-1",
        config_path=path / ".lab-tracker" / "repo.json",
    )


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
    assert report_payload["action"] == "captured"
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


def test_repo_cli_finish_with_artifacts(tmp_path, monkeypatch, capsys) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    (tmp_path / "results.csv").write_bytes(b"a,b\n1,2\n")
    monkeypatch.chdir(tmp_path)
    lt_cli.main(["repo", "init", "--project", "project-1"])
    capsys.readouterr()

    lt_cli.main(
        [
            "repo",
            "finish",
            "--artifact",
            "results.csv",
            "--summary",
            "Decoded stimulus identity.",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["command"] == "repo-finish"
    assert payload["event_type"] == "finish"
    assert payload["artifact_count"] == 1
    event = json.loads(
        (tmp_path / ".lab-tracker" / "outbox" / "repo")
        .glob("*.finish.*.json")
        .__next__()
        .read_text(encoding="utf-8")
    )
    assert event["artifacts"][0]["title"] == "results.csv"
    assert event["artifacts"][0]["content_hash"].startswith("sha256:")
    assert event["summary"] == "Decoded stimulus identity."


def test_repo_cli_report_artifact_missing_file_raises(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    lt_cli.main(["repo", "init", "--project", "project-1"])

    with pytest.raises(LTValidationError, match="not found"):
        lt_cli.main(["repo", "report", "--artifact", "missing.csv"])


# --- hook installer ---------------------------------------------------------


def test_install_hook_creates_managed_block(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    _init_repo_config(tmp_path)

    result = install_post_commit_hook(tmp_path, lt_command="/opt/venv/bin/lt")

    hook = tmp_path / ".git" / "hooks" / "post-commit"
    content = hook.read_text(encoding="utf-8")
    assert result["action"] == "installed"
    assert content.startswith("#!/usr/bin/env sh")
    assert HOOK_BEGIN_MARKER in content
    assert HOOK_END_MARKER in content
    assert "LT='/opt/venv/bin/lt'" in content  # sh-single-quoted, injection-proof
    assert "repo report >" in content
    # The config the hook needs is pinned at install time.
    assert "repo.json" in content
    assert "LAB_TRACKER_REPO_CONFIG" in content
    assert hook.stat().st_mode & 0o111  # executable


def test_install_hook_requires_repo_config(tmp_path, monkeypatch) -> None:
    """A hook installed without a config would no-op on every commit — refuse."""

    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)

    with pytest.raises(LTValidationError, match="lt repo init"):
        install_post_commit_hook(tmp_path, lt_command="lt")


def test_install_hook_is_idempotent_and_updates_in_place(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    _init_repo_config(tmp_path)

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
    _init_repo_config(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho custom hook\nexit 0\n", encoding="utf-8")

    with pytest.raises(LTValidationError):
        install_post_commit_hook(tmp_path, lt_command="lt")

    merged = install_post_commit_hook(tmp_path, lt_command="lt", force=True)
    content = hook.read_text(encoding="utf-8")
    assert merged["action"] == "prepended"
    assert "echo custom hook" in content  # foreign hook preserved
    # The managed block must run BEFORE the foreign body, so a trailing
    # `exit 0`/`exec` there cannot turn provenance capture into dead code.
    assert content.index(HOOK_END_MARKER) < content.index("echo custom hook")


def test_install_hook_refuses_non_sh_foreign_hook(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    _init_repo_config(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/usr/bin/env python3\nprint('custom')\n", encoding="utf-8")

    # Merging sh code into a python hook would syntax-error every commit.
    with pytest.raises(LTValidationError, match="non-sh interpreter"):
        install_post_commit_hook(tmp_path, lt_command="lt", force=True)


def test_install_hook_rejects_corrupted_markers(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    _init_repo_config(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    # END before BEGIN (e.g. a botched merge) must not become a silent no-op.
    hook.write_text(
        f"#!/bin/sh\n{HOOK_END_MARKER}\necho x\n{HOOK_BEGIN_MARKER}\n",
        encoding="utf-8",
    )

    with pytest.raises(LTValidationError, match="out of order"):
        install_post_commit_hook(tmp_path, lt_command="lt")


def test_install_hook_rejects_non_utf8_foreign_hook(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    _init_repo_config(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_bytes(b"#!/bin/sh\n# ma\xeetre hook by Ren\xe9\n")

    with pytest.raises(LTValidationError, match="not UTF-8"):
        install_post_commit_hook(tmp_path, lt_command="lt")


def test_install_hook_quotes_shell_metacharacters_in_lt_path(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    _init_repo_config(tmp_path)

    install_post_commit_hook(tmp_path, lt_command="/opt/john$doe/`hostname`/lt")

    content = (tmp_path / ".git" / "hooks" / "post-commit").read_text(encoding="utf-8")
    # The raw path sits inside single quotes, so $doe cannot expand and the
    # backticked command substitution cannot execute at commit time.
    assert "LT='/opt/john$doe/`hostname`/lt'" in content


def test_install_hook_outside_git_repo_raises(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)

    with pytest.raises(LTValidationError):
        install_post_commit_hook(tmp_path)


def test_install_hook_converts_windows_paths_for_sh(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    _init_repo_config(tmp_path)

    install_post_commit_hook(tmp_path, lt_command="C:\\venv\\Scripts\\lt.exe")

    content = (tmp_path / ".git" / "hooks" / "post-commit").read_text(encoding="utf-8")
    assert "LT='C:/venv/Scripts/lt.exe'" in content
    assert "\\" not in content.split(HOOK_BEGIN_MARKER, 1)[1]


def test_hook_command_path_defaults_to_resolver_sibling(tmp_path, monkeypatch) -> None:
    # With --lt-command omitted, the baked path matches the shared resolver: the
    # lt beside the running interpreter wins over a different PATH lt, so the
    # repo hook agrees with the graph-draft and schedule hooks.
    import shutil

    bindir = "Scripts" if sys.platform == "win32" else "bin"
    py = "python.exe" if sys.platform == "win32" else "python"
    exe = "lt.exe" if sys.platform == "win32" else "lt"
    env_bin = tmp_path / "env" / bindir
    env_bin.mkdir(parents=True)
    (env_bin / py).write_text("", encoding="utf-8")
    sibling = env_bin / exe
    sibling.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(env_bin / py))
    monkeypatch.setattr(shutil, "which", lambda _n: "/some/other/lt")

    assert _hook_command_path(None) == str(sibling).replace("\\", "/")


def test_hook_command_path_falls_back_to_literal_lt(tmp_path, monkeypatch) -> None:
    # Last resort when neither a sibling nor a PATH lt resolves: defer to the
    # commit-time shell's PATH via the bare name (preserves the old behavior).
    import shutil

    empty_bin = tmp_path / "empty" / ("Scripts" if sys.platform == "win32" else "bin")
    empty_bin.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(empty_bin / "python"))
    monkeypatch.setattr(shutil, "which", lambda _n: None)

    assert _hook_command_path(None) == "lt"


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
def test_forced_hook_runs_before_foreign_exit_end_to_end(tmp_path, monkeypatch) -> None:
    """With a foreign hook ending in `exit 0`, capture must still happen."""

    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    lt_cli.main(["repo", "init", "--project", "project-1"])
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho custom hook\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    lt_shim = tmp_path / "lt-shim"
    lt_shim.write_text(
        "#!/usr/bin/env sh\n"
        f'exec "{sys.executable}" -m lab_tracker_client "$@"\n',
        encoding="utf-8",
    )
    lt_shim.chmod(0o755)
    install_post_commit_hook(tmp_path, lt_command=str(lt_shim), force=True)

    (tmp_path / "analysis.py").write_text("print('v4')\n", encoding="utf-8")
    _git(tmp_path, "add", "analysis.py")
    _git(tmp_path, "commit", "-q", "-m", "capture survives foreign exit 0")
    commit = _git(tmp_path, "rev-parse", "HEAD")

    outbox = tmp_path / ".lab-tracker" / "outbox" / "repo"
    events = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(outbox.glob("*.json"))]
    assert any(e["source"]["git_commit"] == commit for e in events)


@pytest.mark.skipif(sys.platform == "win32", reason="sh hook execution test is POSIX-only")
def test_hook_never_blocks_commit_when_lt_fails(tmp_path, monkeypatch) -> None:
    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    _init_repo_config(tmp_path)
    # lt itself is broken (exit 1) -> report fails; the commit must still succeed.
    broken_lt = tmp_path / "broken-lt"
    broken_lt.write_text("#!/usr/bin/env sh\nexit 1\n", encoding="utf-8")
    broken_lt.chmod(0o755)
    install_post_commit_hook(tmp_path, lt_command=str(broken_lt))

    (tmp_path / "analysis.py").write_text("print('v3')\n", encoding="utf-8")
    _git(tmp_path, "add", "analysis.py")
    _git(tmp_path, "commit", "-q", "-m", "commit survives broken hook")

    assert _git(tmp_path, "log", "-1", "--pretty=%s") == "commit survives broken hook"


# --- cross-adapter coexistence (lt-81s6.17) ----------------------------------


def test_install_hook_refuses_graph_draft_hook_without_force(tmp_path, monkeypatch) -> None:
    """Two Lab Tracker capture hooks would record every commit twice."""

    from lab_tracker_client.hooks import HOOK_BLOCK_BEGIN, HOOK_BLOCK_END

    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    _init_repo_config(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        f"#!/usr/bin/env sh\n{HOOK_BLOCK_BEGIN}\n: draft capture\n{HOOK_BLOCK_END}\n",
        encoding="utf-8",
    )

    with pytest.raises(LTValidationError, match="lt git snapshot"):
        install_post_commit_hook(tmp_path, lt_command="lt")

    forced = install_post_commit_hook(tmp_path, lt_command="lt", force=True)
    content = hook.read_text(encoding="utf-8")
    assert forced["action"] == "prepended"
    assert HOOK_BLOCK_BEGIN in content  # foreign draft block preserved
    assert HOOK_BEGIN_MARKER in content


def test_install_hook_updates_own_block_despite_draft_block(tmp_path, monkeypatch) -> None:
    """A deliberately-forced dual setup must stay idempotently updatable."""

    from lab_tracker_client.hooks import HOOK_BLOCK_BEGIN, HOOK_BLOCK_END

    _clear_repo_env(monkeypatch)
    _init_git_repo(tmp_path)
    _init_repo_config(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        f"#!/usr/bin/env sh\n{HOOK_BLOCK_BEGIN}\n: draft capture\n{HOOK_BLOCK_END}\n",
        encoding="utf-8",
    )
    install_post_commit_hook(tmp_path, lt_command="/old/lt", force=True)

    # Re-running WITHOUT force updates our own block in place.
    result = install_post_commit_hook(tmp_path, lt_command="/new/lt")

    content = hook.read_text(encoding="utf-8")
    assert result["action"] == "updated"
    assert "/new/lt" in content
    assert "/old/lt" not in content
    assert HOOK_BLOCK_BEGIN in content  # draft block untouched
