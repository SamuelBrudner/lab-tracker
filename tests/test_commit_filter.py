"""The shared commit filter: which commits a post-commit capture leaves out."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lab_tracker_client.client import LTValidationError
from lab_tracker_client.gitinfo import (
    SKIP_REASON_FIXUP,
    SKIP_REASON_MERGE,
    SKIP_REASON_PATHS,
    SKIP_REASON_WIP,
    CommitFilter,
    commit_skip_reason,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _commit(repo: Path, name: str, text: str, subject: str) -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "commit.gpgsign", "false")
    _commit(path, "analysis.py", "print('one')\n", "first analysis")
    return path


def _merge_commit(repo: Path) -> str:
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "feature.py", "print('feature')\n", "feature work")
    _git(repo, "checkout", "-q", "-")
    _commit(repo, "main.py", "print('main')\n", "main work")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge feature", "feature")
    return _git(repo, "rev-parse", "HEAD")


def test_default_filter_skips_merge_commits(repo: Path) -> None:
    merge = _merge_commit(repo)

    assert commit_skip_reason(repo, merge, CommitFilter()) == SKIP_REASON_MERGE
    assert commit_skip_reason(repo, merge, CommitFilter(skip_merges=False)) == ""
    # The merged work itself is still recorded.
    assert commit_skip_reason(repo, f"{merge}^2", CommitFilter()) == ""


def test_default_filter_skips_fixup_and_squash_subjects(repo: Path) -> None:
    fixup = _commit(repo, "analysis.py", "print('two')\n", "fixup! first analysis")
    squash = _commit(repo, "analysis.py", "print('three')\n", "squash! first analysis")

    assert commit_skip_reason(repo, fixup, CommitFilter()) == SKIP_REASON_FIXUP
    assert commit_skip_reason(repo, squash, CommitFilter()) == SKIP_REASON_FIXUP
    assert commit_skip_reason(repo, fixup, CommitFilter(skip_fixups=False)) == ""


def test_wip_is_opt_in(repo: Path) -> None:
    wip = _commit(repo, "analysis.py", "print('wip')\n", "wip: tinkering")
    upper = _commit(repo, "analysis.py", "print('WIP')\n", "WIP tweak")
    wipe = _commit(repo, "analysis.py", "print('wipe')\n", "wipe the table")

    assert commit_skip_reason(repo, wip, CommitFilter()) == ""
    assert commit_skip_reason(repo, wip, CommitFilter(skip_wip=True)) == SKIP_REASON_WIP
    assert commit_skip_reason(repo, upper, CommitFilter(skip_wip=True)) == SKIP_REASON_WIP
    # A word that merely starts with "wip" is not a wip marker.
    assert commit_skip_reason(repo, wipe, CommitFilter(skip_wip=True)) == ""


def test_path_globs_skip_only_when_every_changed_path_matches(repo: Path) -> None:
    docs_only = _commit(repo, "docs/x.md", "# x\n", "docs: x")
    (repo / "docs" / "y.md").write_text("# y\n", encoding="utf-8")
    (repo / "src" / "a.py").parent.mkdir(parents=True)
    (repo / "src" / "a.py").write_text("print('a')\n", encoding="utf-8")
    _git(repo, "add", "docs/y.md", "src/a.py")
    _git(repo, "commit", "-q", "-m", "docs and code")
    mixed = _git(repo, "rev-parse", "HEAD")
    only_docs = CommitFilter(skip_path_globs=("docs/*",))

    assert commit_skip_reason(repo, docs_only, only_docs) == SKIP_REASON_PATHS
    assert commit_skip_reason(repo, mixed, only_docs) == ""
    assert commit_skip_reason(repo, docs_only, CommitFilter()) == ""


def test_unknown_commit_is_never_skipped(repo: Path) -> None:
    assert commit_skip_reason(repo, "0" * 40, CommitFilter()) == ""
    assert commit_skip_reason(repo / "missing", "HEAD", CommitFilter()) == ""


def test_from_mapping_rejects_unknown_keys_and_wrong_types() -> None:
    with pytest.raises(LTValidationError, match="skip_marges"):
        CommitFilter.from_mapping({"skip_marges": True})
    with pytest.raises(LTValidationError, match="skip_wip must be true or false"):
        CommitFilter.from_mapping({"skip_wip": "yes"})
    with pytest.raises(LTValidationError, match="skip_path_globs"):
        CommitFilter.from_mapping({"skip_path_globs": "docs/*"})
    with pytest.raises(LTValidationError, match="skip_path_globs"):
        CommitFilter.from_mapping({"skip_path_globs": [""]})
    with pytest.raises(LTValidationError, match="JSON object"):
        CommitFilter.from_mapping(["skip_wip"])  # type: ignore[arg-type]


def test_from_mapping_round_trips_to_dict() -> None:
    assert CommitFilter.from_mapping({}) == CommitFilter()
    custom = CommitFilter(skip_merges=False, skip_wip=True, skip_path_globs=("docs/*", "*.md"))

    assert CommitFilter.from_mapping(custom.to_dict()) == custom
    assert custom.to_dict() == {
        "skip_merges": False,
        "skip_fixups": True,
        "skip_wip": True,
        "skip_path_globs": ["docs/*", "*.md"],
    }
