"""Every capture adapter stamps the code identity into the synced NOTE metadata.

The contract (shared with the server's ``worktree_tree_match`` detector):

* ``run_git_worktree_tree`` -- ``run_context`` figure captures (and ``lt run``);
* ``capture_git_worktree_tree`` -- plain figure/file captures in a checkout;
* ``hpc_git_worktree_tree`` -- ``lt hpc begin``/``finish`` events;
* ``repo_git_tree`` -- ``lt repo`` commit events (the commit's own tree).

Each test follows the value from the capture all the way to the metadata the
server receives on ``/notes/upload-file``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

import lab_tracker_client.figure as figure_module
from lab_tracker_client import LabTracker, gitinfo, run_context, savefig
from lab_tracker_client import hpc as hpc_capture
from lab_tracker_client import repo as repo_capture
from lab_tracker_client.figure import _reset_figure_capture_state_for_tests


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _multipart_field(body: bytes, name: str) -> str:
    marker = f'name="{name}"'.encode()
    assert marker in body
    chunk = body.split(marker, 1)[1]
    chunk = chunk.split(b"\r\n\r\n", 1)[1]
    return chunk.split(b"\r\n--", 1)[0].decode("utf-8")


class _Server:
    """A MockTransport that lists no notes and records every uploaded note's metadata."""

    def __init__(self) -> None:
        self.uploads: list[dict[str, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/notes":
            return httpx.Response(
                200, json={"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            metadata = json.loads(_multipart_field(request.content, "metadata"))
            self.uploads.append(metadata)
            return httpx.Response(
                201,
                json={
                    "data": {
                        "note_id": f"note-{len(self.uploads)}",
                        "project_id": "project-1",
                        "status": "staged",
                        "metadata": metadata,
                    }
                },
            )
        return httpx.Response(500, json={"error": {"message": "unexpected request"}})

    def client(self, **kwargs: object) -> LabTracker:
        return LabTracker(
            base_url="http://testserver",
            transport=httpx.MockTransport(self.handler),
            **kwargs,  # type: ignore[arg-type]
        )


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for name in (
        "LAB_TRACKER_PROJECT_ID",
        "LAB_TRACKER_BASE_URL",
        "LAB_TRACKER_MCP_BASE_URL",
        "LAB_TRACKER_SESSION_ID",
        "LAB_TRACKER_HPC_CONFIG",
        "LAB_TRACKER_HPC_OUTBOX",
        "LAB_TRACKER_HPC_RUN_ID",
        "LAB_TRACKER_REPO_CONFIG",
        "LAB_TRACKER_REPO_OUTBOX",
        "LAB_TRACKER_REPO_RUN_ID",
        "LAB_TRACKER_WORKTREE_TREE",
        "LAB_TRACKER_GIT_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LAB_TRACKER_WATCH_OUTBOX", str(tmp_path / "watch-outbox"))
    _reset_figure_capture_state_for_tests()
    gitinfo._reset_worktree_tree_cache_for_tests()
    yield
    _reset_figure_capture_state_for_tests()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "analysis"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text(".lab-tracker/\n", encoding="utf-8")
    (root / "analysis.py").write_text("print('v1')\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial analysis")
    return root


def _head_tree(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD^{tree}")


class _Figure:
    def __init__(self, payload: bytes = b"png-bytes") -> None:
        self.payload = payload

    def savefig(self, path: str | Path, **_kwargs: object) -> None:
        Path(path).write_bytes(self.payload)


# --- figure captures ----------------------------------------------------------


def test_a_figure_saved_in_a_checkout_carries_its_code_tree_without_itself(
    repo: Path,
) -> None:
    server = _Server()
    (repo / "figures").mkdir()

    with server.client(default_project_id="project-1") as lt:
        first = savefig(_Figure(b"one"), repo / "figures" / "trace.png", client=lt)
        # Re-saving the same (untracked) output never changes the code identity.
        second = savefig(_Figure(b"two"), repo / "figures" / "trace.png", client=lt)

    assert first.action == "imported"
    assert second.action in {"imported", "coalesced"}
    assert server.uploads[0]["capture_git_worktree_tree"] == _head_tree(repo)
    assert first.metadata["capture_git_worktree_tree"] == _head_tree(repo)
    assert second.metadata["capture_git_worktree_tree"] == _head_tree(repo)


def test_a_figure_saved_from_uncommitted_code_names_that_code(repo: Path) -> None:
    server = _Server()
    (repo / "analysis.py").write_text("print('v2 uncommitted')\n", encoding="utf-8")

    with server.client(default_project_id="project-1") as lt:
        savefig(_Figure(), repo / "trace.png", client=lt)

    stamped = server.uploads[0]["capture_git_worktree_tree"]
    assert stamped != _head_tree(repo)
    _git(repo, "add", "analysis.py")
    _git(repo, "commit", "-q", "-m", "the code that made the figure")
    assert stamped == _head_tree(repo)


def test_a_figure_saved_outside_any_checkout_has_no_code_tree(tmp_path: Path) -> None:
    server = _Server()
    loose = tmp_path / "loose"
    loose.mkdir()

    with server.client(default_project_id="project-1") as lt:
        savefig(_Figure(), loose / "trace.png", client=lt)

    assert "capture_git_worktree_tree" not in server.uploads[0]
    assert "capture_git_worktree_tree_error" not in server.uploads[0]


def test_a_failing_tree_computation_never_fails_the_capture(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _Server()

    def explode(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("tree exploded")

    monkeypatch.setattr(figure_module, "worktree_tree_id", explode)
    with server.client(default_project_id="project-1") as lt:
        result = savefig(_Figure(), repo / "trace.png", client=lt)

    assert result.action == "imported"
    assert "capture_git_worktree_tree" not in server.uploads[0]


def test_an_offline_figure_keeps_its_code_tree_through_the_outbox(
    repo: Path, tmp_path: Path
) -> None:
    from lab_tracker_client.watch import sync_outbox_path

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(offline),
    ) as lt:
        queued = savefig(_Figure(), repo / "trace.png", client=lt)
    assert queued.action == "queued"

    server = _Server()
    with server.client() as lt:
        summary = sync_outbox_path(lt, tmp_path / "watch-outbox", default_project_id="project-1")

    assert summary["errors"] == []
    assert server.uploads[0]["capture_git_worktree_tree"] == _head_tree(repo)


def test_run_context_stamps_the_working_copy_tree(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(repo)
    server = _Server()
    outputs = tmp_path / "outputs"
    outputs.mkdir()

    with server.client(default_project_id="project-1") as lt, run_context() as context:
        savefig(_Figure(), outputs / "trace.png", client=lt)

    assert context.to_metadata()["run_git_worktree_tree"] == _head_tree(repo)
    assert server.uploads[0]["run_git_worktree_tree"] == _head_tree(repo)

    (repo / "analysis.py").write_text("print('v2')\n", encoding="utf-8")
    with run_context() as dirty:
        assert dirty.to_metadata()["run_git_worktree_tree"] != _head_tree(repo)


def test_run_context_records_why_the_tree_is_unknown(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setenv("LAB_TRACKER_WORKTREE_TREE", "0")

    with run_context() as context:
        metadata = context.to_metadata()

    assert "run_git_worktree_tree" not in metadata
    assert metadata["run_git_worktree_tree_error"] == "disabled"


# --- hpc ------------------------------------------------------------------------


def test_hpc_begin_and_finish_carry_the_worktree_tree_to_the_note(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(repo)
    (repo / "analysis.py").write_text("print('job edit')\n", encoding="utf-8")
    config = hpc_capture.HpcConfig(
        project_id="project-1", cluster="test-cluster", outbox=str(tmp_path / "hpc-outbox")
    )

    begin, _begin_path = hpc_capture.begin_event(config, run_id="run-1")
    finish, _finish_path = hpc_capture.finish_event(config, run_id="run-1", exit_code=0)
    tree = begin["source"]["git_worktree_tree"]
    server = _Server()
    with server.client() as lt:
        summary = hpc_capture.sync_outbox(lt, config)

    assert summary["errors"] == []
    assert len(tree) == 40 and tree != _head_tree(repo)
    assert finish["source"]["git_worktree_tree"] == tree
    assert [upload["hpc_git_worktree_tree"] for upload in server.uploads] == [tree, tree]
    assert f"- Git worktree tree: `{tree}`" in hpc_capture.render_event_note(begin)


def test_hpc_submit_events_are_unchanged(repo: Path, tmp_path: Path) -> None:
    config = hpc_capture.HpcConfig(
        project_id="project-1", cluster="test-cluster", outbox=str(tmp_path / "hpc-outbox")
    )

    event = hpc_capture.make_event(config, event_type="submit", run_id="run-1", cwd=repo)

    assert "git_worktree_tree" not in event["source"]


# --- repo -----------------------------------------------------------------------


def test_repo_commit_events_carry_the_commits_own_tree_to_the_note(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    # An uncommitted edit must not leak into the commit's identity.
    (repo / "analysis.py").write_text("print('later edit')\n", encoding="utf-8")
    config = repo_capture.init_config(project_id="project-1")

    event, _path, _action = repo_capture.capture_commit(config)
    finish, _finish_path, _finish_action = repo_capture.capture_commit(config, event_type="finish")
    server = _Server()
    with server.client() as lt:
        summary = repo_capture.sync_outbox(lt, config)

    assert summary["errors"] == []
    assert event["source"]["git_tree"] == _head_tree(repo)
    assert "git_tree" not in finish["source"]
    by_type = {str(upload["repo_event_type"]): upload for upload in server.uploads}
    assert by_type["commit"]["repo_git_tree"] == _head_tree(repo)
    assert "repo_git_tree" not in by_type["finish"]
