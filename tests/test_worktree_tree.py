"""Identity for uncommitted code: the git tree id of the current working copy.

``gitinfo.worktree_tree_id`` answers "which exact code produced this capture"
even when the code was never committed: it is the tree ``git add -A && git
commit`` would record right now. It must never touch the user's real index or
object store, never raise into a capture, and stay bounded.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lab_tracker_client import gitinfo
from lab_tracker_client.gitinfo import (
    GIT_TIMEOUT_ENV,
    WORKTREE_TREE_ENV,
    WORKTREE_TREE_ERROR_DISABLED,
    WORKTREE_TREE_ERROR_GIT_UNAVAILABLE,
    WORKTREE_TREE_ERROR_TIMEOUT,
    WORKTREE_TREE_ERROR_TOO_LARGE,
    WorktreeTree,
    commit_tree_id,
    worktree_tree_id,
)

_REAL_GIT = shutil.which("git")


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch):
    for name in (WORKTREE_TREE_ENV, GIT_TIMEOUT_ENV):
        monkeypatch.delenv(name, raising=False)
    gitinfo._reset_worktree_tree_cache_for_tests()
    yield
    gitinfo._reset_worktree_tree_cache_for_tests()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "analysis"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (root / "analysis.py").write_text("print('v1')\n", encoding="utf-8")
    (root / "lib").mkdir()
    (root / "lib" / "helpers.py").write_text("X = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial analysis")
    return root


def _head_tree(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD^{tree}")


def _object_files(root: Path) -> list[Path]:
    return sorted(path for path in (root / ".git" / "objects").rglob("*") if path.is_file())


def test_a_clean_checkout_is_its_head_tree(repo: Path) -> None:
    result = worktree_tree_id(repo)

    assert result == WorktreeTree(tree=_head_tree(repo), clean=True)
    assert result.as_fields("run_git_worktree_tree") == {"run_git_worktree_tree": _head_tree(repo)}


def test_ignored_files_do_not_change_the_tree(repo: Path) -> None:
    (repo / "ignored").mkdir()
    (repo / "ignored" / "cache.bin").write_bytes(b"\0" * 1024)

    assert worktree_tree_id(repo).tree == _head_tree(repo)


def test_a_dirty_checkout_is_the_tree_a_commit_of_everything_would_record(repo: Path) -> None:
    (repo / "analysis.py").write_text("print('v2 uncommitted')\n", encoding="utf-8")
    (repo / "lib" / "new_module.py").write_text("Y = 2\n", encoding="utf-8")

    dirty = worktree_tree_id(repo)

    assert dirty.tree and dirty.tree != _head_tree(repo)
    assert dirty.clean is False
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "commit everything")
    assert _head_tree(repo) == dirty.tree


def test_untracked_files_alone_change_the_tree(repo: Path) -> None:
    (repo / "notes.md").write_text("draft\n", encoding="utf-8")

    assert worktree_tree_id(repo).tree != _head_tree(repo)


def test_the_real_index_and_object_store_are_untouched(repo: Path) -> None:
    (repo / "analysis.py").write_text("print('v2 uncommitted')\n", encoding="utf-8")
    (repo / "fresh.py").write_text("Z = 3\n", encoding="utf-8")
    # A plain `git status` may refresh the index itself, so take it first.
    status_before = _git(repo, "status", "--porcelain")
    index = repo / ".git" / "index"
    index_bytes = index.read_bytes()
    index_mtime = index.stat().st_mtime_ns
    objects_before = _object_files(repo)

    result = worktree_tree_id(repo)

    assert result.tree
    assert index.read_bytes() == index_bytes
    assert index.stat().st_mtime_ns == index_mtime
    assert _object_files(repo) == objects_before
    assert _git(repo, "status", "--porcelain") == status_before
    # The tree was built in a scratch object store, so the real one cannot
    # resolve it: nothing was written into the user's repository.
    missing = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", result.tree], capture_output=True
    )
    assert missing.returncode != 0


def test_a_staged_change_reverted_in_the_working_copy_is_clean_again(repo: Path) -> None:
    (repo / "analysis.py").write_text("print('staged')\n", encoding="utf-8")
    _git(repo, "add", "analysis.py")
    (repo / "analysis.py").write_text("print('v1')\n", encoding="utf-8")

    result = worktree_tree_id(repo)

    assert result.tree == _head_tree(repo)
    assert result.clean is True


def test_excluded_paths_and_lab_tracker_scratch_do_not_count_as_code(repo: Path) -> None:
    (repo / "figures").mkdir()
    figure = repo / "figures" / "trace.png"
    figure.write_bytes(b"png")
    (repo / ".lab-tracker" / "outbox" / "watch").mkdir(parents=True)
    (repo / ".lab-tracker" / "outbox" / "watch" / "event.json").write_text("{}", encoding="utf-8")

    assert worktree_tree_id(repo, exclude=[figure]).tree == _head_tree(repo)
    assert worktree_tree_id(repo, exclude=[repo / "figures"]).tree == _head_tree(repo)
    assert worktree_tree_id(repo).tree != _head_tree(repo)


def test_an_excluded_tracked_file_keeps_its_committed_content(repo: Path) -> None:
    (repo / "results.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    _git(repo, "add", "results.csv")
    _git(repo, "commit", "-q", "-m", "results")
    (repo / "results.csv").write_text("a,b\n3,4\n", encoding="utf-8")

    assert worktree_tree_id(repo, exclude=[repo / "results.csv"]).tree == _head_tree(repo)


def test_repeated_calls_reuse_the_cached_tree_until_the_working_copy_changes(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "analysis.py").write_text("print('v2')\n", encoding="utf-8")
    builds: list[Path] = []
    real_build = gitinfo._tree_from_temporary_index

    def counting(toplevel, *args, **kwargs):
        builds.append(toplevel)
        return real_build(toplevel, *args, **kwargs)

    monkeypatch.setattr(gitinfo, "_tree_from_temporary_index", counting)

    first = worktree_tree_id(repo)
    second = worktree_tree_id(repo)
    assert first == second
    assert len(builds) == 1

    # Same status line (" M analysis.py"), different content: rebuilt.
    (repo / "analysis.py").write_text("print('v3 is longer')\n", encoding="utf-8")
    third = worktree_tree_id(repo)
    assert third.tree != first.tree
    assert len(builds) == 2


def test_huge_untracked_content_is_skipped_as_too_large(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gitinfo, "WORKTREE_TREE_MAX_BYTES", 1024)
    (repo / "dump.bin").write_bytes(b"\1" * 4096)

    result = worktree_tree_id(repo)

    assert result.tree == ""
    assert result.error == WORKTREE_TREE_ERROR_TOO_LARGE
    assert result.as_fields("run_git_worktree_tree") == {
        "run_git_worktree_tree_error": WORKTREE_TREE_ERROR_TOO_LARGE
    }


def test_too_many_changed_files_are_skipped_as_too_large(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gitinfo, "WORKTREE_TREE_MAX_FILES", 3)
    for index in range(5):
        (repo / f"scratch_{index}.txt").write_text(str(index), encoding="utf-8")

    assert worktree_tree_id(repo).error == WORKTREE_TREE_ERROR_TOO_LARGE


def test_outside_a_checkout_there_is_nothing_to_record(tmp_path: Path) -> None:
    loose = tmp_path / "loose"
    loose.mkdir()

    assert worktree_tree_id(loose) == WorktreeTree()
    assert worktree_tree_id(tmp_path / "missing") == WorktreeTree()
    assert WorktreeTree().as_fields("capture_git_worktree_tree") == {}


def test_the_kill_switch_disables_the_computation(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(WORKTREE_TREE_ENV, "off")

    assert worktree_tree_id(repo).error == WORKTREE_TREE_ERROR_DISABLED


def test_git_missing_is_an_error_marker_not_an_exception(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "no-git-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    assert worktree_tree_id(repo).error == WORKTREE_TREE_ERROR_GIT_UNAVAILABLE


@pytest.mark.skipif(os.name == "nt" or _REAL_GIT is None, reason="POSIX git shim")
def test_a_hanging_git_is_bounded_by_the_timeout(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _REAL_GIT is not None
    bin_dir = tmp_path / "hang-bin"
    bin_dir.mkdir()
    shim = bin_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = status ]; then exec sleep 30; fi\n'
        "done\n"
        f'exec "{_REAL_GIT}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{Path(_REAL_GIT).parent}")

    result = worktree_tree_id(repo, timeout=0.5)

    assert result.tree == ""
    assert result.error == WORKTREE_TREE_ERROR_TIMEOUT


def test_an_invalid_git_timeout_setting_never_raises(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GIT_TIMEOUT_ENV, "soon")

    assert worktree_tree_id(repo).tree == _head_tree(repo)


def test_an_unborn_checkout_still_has_a_worktree_tree(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "first.py").write_text("A = 1\n", encoding="utf-8")

    result = worktree_tree_id(root)

    assert len(result.tree) == 40
    assert result.error == ""


def test_commit_tree_id_names_the_commits_own_tree(repo: Path, tmp_path: Path) -> None:
    commit = _git(repo, "rev-parse", "HEAD")

    assert commit_tree_id(repo, commit) == _head_tree(repo)
    assert commit_tree_id(repo, "0" * 40) == ""
    assert commit_tree_id(tmp_path, commit) == ""
