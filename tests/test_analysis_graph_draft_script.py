from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import httpx
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


# --- remote-URL credential hygiene (H9/H10 CI path) ---------------------------

SECRET = "SEKRET_TOKEN_123"

# Same remote forms as tests/test_repo_remote_urls.py: the inlined sanitiser
# must agree with lab_tracker_client.gitinfo.sanitize_remote_url on all of them.
REMOTE_FORMS = [
    "https://github.com/lab/repo.git",
    "http://example.com:8080/lab/repo",
    "git@github.com:lab/repo.git",
    "github.com:lab/repo.git",
    "ssh://git@github.com/lab/repo.git",
    "ssh://git@github.com:2222/lab/repo.git",
    "git://example.com/lab/repo.git",
    "file:///srv/git/repo.git",
    "/srv/git/repo.git",
    "../sibling/repo",
    "",
    "  https://github.com/lab/repo.git\n",
    f"https://oauth2:{SECRET}@gitlab.example.com/lab/repo.git",
    f"https://user:{SECRET}@example.com/lab/repo.git",
    f"https://{SECRET}@github.com/lab/repo.git",
    f"HTTPS://{SECRET}@github.com/lab/repo.git",
    f"http://x-access-token:{SECRET}@example.com:8080/lab/repo",
    f"https://user:p@ss{SECRET}@example.com/lab/repo.git",
    f"https://github.com/lab/repo.git?access_token={SECRET}",
    f"https://github.com/lab/repo.git#{SECRET}",
    f"https://sam:{SECRET}@github.com/lab/repo.git?private_token={SECRET}#frag",
    f"ssh://git:{SECRET}@example.com/lab/repo.git",
    f"git+ssh://deploy:{SECRET}@example.com:22/lab/repo",
    f"ssh://:{SECRET}@example.com/lab/repo",
    f"git://{SECRET}@example.com/lab/repo.git",
    "deploy@example.com:lab/repo@v2.git",
]


@pytest.mark.parametrize("remote", REMOTE_FORMS)
def test_sanitize_remote_url_contract_between_client_and_ci_script(remote: str) -> None:
    """The script's inlined sanitiser is pinned to the client package's."""

    from lab_tracker_client.gitinfo import sanitize_remote_url

    script = _load_script()

    assert script._sanitize_remote_url(remote) == sanitize_remote_url(remote)
    assert SECRET not in script._sanitize_remote_url(remote)


CREDENTIALED_REMOTES = [
    f"https://oauth2:{SECRET}@gitlab.example.com/lab/repo.git",
    f"https://{SECRET}@github.com/lab/repo.git",
    f"https://github.com/lab/repo.git?access_token={SECRET}",
    f"ssh://git:{SECRET}@example.com/lab/repo.git",
]


def _repo_with_remote(tmp_path: Path, remote: str) -> Path:
    repo = tmp_path / "analysis-repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "remote", "add", "origin", remote)
    (repo / "analysis.md").write_text("Result.\n", encoding="utf-8")
    _git(repo, "add", "analysis.md")
    _git(repo, "commit", "-m", "Add result")
    return repo


@pytest.mark.parametrize("remote", CREDENTIALED_REMOTES)
def test_git_commit_evidence_never_records_remote_credentials(
    tmp_path: Path, remote: str
) -> None:
    from lab_tracker_client.gitinfo import sanitize_remote_url
    from lab_tracker_client.repo import normalize_remote

    script = _load_script()
    repo = _repo_with_remote(tmp_path, remote)

    evidence, metadata = script._git_commit_evidence(
        repo, "HEAD", max_diff_lines=80, context_lines=1
    )

    clean = sanitize_remote_url(remote)
    assert SECRET not in evidence
    assert SECRET not in json.dumps(metadata)
    assert f"- remote_origin: {clean}" in evidence
    assert metadata["git_remote_origin_url"] == clean
    assert metadata["evidence_source_uri"] == clean
    # Same identity the client adapter derives from its sanitised remote.
    assert metadata["evidence_source_external_id"] == (
        f"{normalize_remote(clean)}@{metadata['git_commit']}"
    )


@pytest.mark.parametrize("remote", CREDENTIALED_REMOTES)
def test_created_note_payload_never_contains_remote_credentials(
    tmp_path: Path, remote: str
) -> None:
    script = _load_script()
    repo = _repo_with_remote(tmp_path, remote)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"data": {"note_id": "note-1"}})

    args = argparse.Namespace(
        evidence_file=None,
        git_repo=repo,
        git_commit="HEAD",
        git_max_diff_lines=80,
        git_context_lines=1,
        metadata_json=None,
        project_id="project-1",
        note_status="committed",
    )
    with httpx.Client(
        base_url="https://lab.example.test", transport=httpx.MockTransport(handler)
    ) as client:
        note_id = script._create_evidence_note(client, args, headers={})

    assert note_id == "note-1"
    assert len(requests) == 1
    body = requests[0].content.decode("utf-8")
    assert json.loads(body)["metadata"]["git_remote_origin_url"]
    assert SECRET not in body
