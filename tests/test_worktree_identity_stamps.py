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
import sys
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


_SLURM_ENV = ("SLURM_JOB_ID", "SLURM_ARRAY_JOB_ID", "SLURM_ARRAY_TASK_ID", "SLURM_SUBMIT_DIR")


def _submit_job(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in _SLURM_ENV:
        monkeypatch.delenv(name, raising=False)
    config = hpc_capture.init_config(
        project_id="project-1",
        cluster="test-cluster",
        outbox=str(tmp_path / "hpc-outbox"),
        config_path=repo / ".lab-tracker" / "hpc.json",
    )
    # An older job's log in the submit folder is not code either.
    (repo / "slurm-1.out").write_text("old job\n", encoding="utf-8")
    result = hpc_capture.run_submit_command(
        config, [sys.executable, "-c", "print('Submitted batch job 4242')"], cwd=repo
    )
    return config, result


def test_hpc_submit_records_the_code_tree_before_sbatch(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "analysis.py").write_text("print('submitted edit')\n", encoding="utf-8")
    expected = gitinfo.worktree_tree_id(repo, exclude=[repo / "slurm-1.out"]).tree

    config, result = _submit_job(repo, tmp_path, monkeypatch)

    submit = json.loads(Path(result["event_path"]).read_text(encoding="utf-8"))
    manifest = json.loads(Path(result["run_manifest"]).read_text(encoding="utf-8"))
    assert expected and expected != _head_tree(repo)
    assert submit["source"]["git_worktree_tree"] == expected
    assert manifest["git_worktree_tree"] == expected
    metadata = hpc_capture.event_metadata(
        submit, source_uri="file:///e.json", source_external_id="x", content_hash="0" * 64
    )
    assert metadata["hpc_git_worktree_tree"] == expected


def test_begin_finish_and_epilog_reuse_the_submitted_tree(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "analysis.py").write_text("print('submitted edit')\n", encoding="utf-8")
    config, result = _submit_job(repo, tmp_path, monkeypatch)
    submitted = json.loads(Path(result["event_path"]).read_text())["source"]["git_worktree_tree"]
    # The job runs later: the code moved on, and Slurm's output file grows.
    (repo / "analysis.py").write_text("print('edited after submit')\n", encoding="utf-8")
    (repo / "slurm-4242.out").write_text("step 1 of 3\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("SLURM_JOB_ID", "4242")
    monkeypatch.setenv("SLURM_SUBMIT_DIR", str(repo))

    begin, _ = hpc_capture.begin_event(config)
    (repo / "slurm-4242.out").write_text("step 1 of 3\ndone\n", encoding="utf-8")
    finish, _ = hpc_capture.finish_event(config, exit_code=0, logs=[repo / "slurm-4242.out"])
    epilog = hpc_capture.epilog_finish(exit_code=0)

    assert begin["run_id"] == result["run_id"] == finish["run_id"]
    assert begin["source"]["git_worktree_tree"] == submitted
    assert finish["source"]["git_worktree_tree"] == submitted
    # The job already finished itself, so the epilog leaves that record alone.
    assert epilog["action"] == "already_finished"


def test_the_epilog_reuses_the_submitted_tree_from_the_outbox(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, result = _submit_job(repo, tmp_path, monkeypatch)
    submitted = json.loads(Path(result["event_path"]).read_text())["source"]["git_worktree_tree"]
    Path(result["run_manifest"]).unlink()
    (repo / "analysis.py").write_text("print('edited after submit')\n", encoding="utf-8")

    finish, _ = hpc_capture.finish_event(
        config, run_id=result["run_id"], exit_code=0, cwd=repo, outbox=Path(result["outbox"])
    )

    assert finish["source"]["git_worktree_tree"] == submitted


def test_a_job_without_a_submitted_tree_leaves_its_own_output_out(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in _SLURM_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(repo)
    (repo / "slurm-77.out").write_text("growing log\n", encoding="utf-8")
    config = hpc_capture.HpcConfig(
        project_id="project-1", cluster="test-cluster", outbox=str(tmp_path / "hpc-outbox")
    )

    begin, _ = hpc_capture.begin_event(config, run_id="run-direct")

    assert begin["source"]["git_worktree_tree"] == _head_tree(repo)
    assert gitinfo.worktree_tree_id(repo).tree != _head_tree(repo)


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
