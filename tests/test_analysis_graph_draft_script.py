from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "create-analysis-graph-draft.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("create_analysis_graph_draft", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_git_commit_evidence_includes_commit_context(tmp_path: Path) -> None:
    script = _load_script()
    repo = tmp_path / "analysis-repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    evidence_file = repo / "analysis.md"
    evidence_file.write_text("Latency decreased after optogenetic stimulation.\n", encoding="utf-8")
    _git(repo, "add", "analysis.md")
    _git(repo, "commit", "-m", "Add optogenetic latency result")

    evidence, metadata = script._git_commit_evidence(
        repo,
        "HEAD",
        max_diff_lines=80,
        context_lines=1,
    )

    assert "# Git Commit Evidence" in evidence
    assert "Add optogenetic latency result" in evidence
    assert "analysis.md" in evidence
    assert "Latency decreased" in evidence
    assert metadata["source"] == "git-commit-analysis-graph-draft"
    assert metadata["git_repository_name"] == "analysis-repo"
    assert len(metadata["git_commit"]) == 40
    assert metadata["git_diff_truncated"] is False
    assert metadata["evidence_source_provider"] == "git"
    # Shared git-evidence identity (<normalized-remote>@<commit>; "local" when
    # no remote is configured) so hook and CI paths dedup to one identity per
    # commit — see lab_tracker_client.repo.event_source_external_id.
    assert metadata["evidence_source_external_id"] == f"local@{metadata['git_commit']}"
    assert metadata["evidence_capture_kind"] == "git_commit"
    assert metadata["evidence_content_hash"]


def test_truncate_lines_marks_omitted_diff_lines() -> None:
    script = _load_script()

    text, truncated = script._truncate_lines("one\ntwo\nthree", 2)

    assert truncated is True
    assert text == "one\ntwo\n... truncated 1 additional diff lines ..."


def test_base_url_normalization_accepts_browser_route_and_rejects_api_path() -> None:
    script = _load_script()

    assert (
        script._normalize_base_url("https://lab.example.test/app/")
        == "https://lab.example.test"
    )
    with pytest.raises(ValueError, match="origin with no path"):
        script._normalize_base_url("https://lab.example.test/api")
