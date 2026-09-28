"""``lt run``: a transparent command wrapper that records what ran as one staged note.

The wrapper's contract, in the order these tests check it:

* argv secrets never reach the event, the note body, or its metadata;
* the command runs with inherited stdio and ``lt run`` exits with exactly the
  command's exit code (128+N for a signal, 127/126 when it cannot start);
* declared output folders are diffed by a before/after snapshot and recorded
  as bounded artifact pointers with sha256 under a size cap;
* one watch-outbox staged-note event per run, carried into the synced note;
* capture never changes the exit code and prints at most one stderr line; an
  unbound project runs the command and captures nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest

from lab_tracker_client import LabTracker, gitinfo, run_capture
from lab_tracker_client import cli as lt_cli
from lab_tracker_client.run_capture import REDACTED, RunOptions, redact_argv, run_command
from lab_tracker_client.watch import read_event, sync_outbox_path

PROJECT_ID = "11111111-2222-4333-8444-555555555555"
QUESTION_ID = "22222222-3333-4444-8555-666666666666"
SESSION_ID = "33333333-4444-4555-8666-777777777777"


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
    def __init__(self) -> None:
        self.uploads: list[dict[str, object]] = []
        self.paths: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.method == "GET" and request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "GET" and request.url.path == "/notes":
            return httpx.Response(
                200, json={"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            body = request.content
            targets = (
                json.loads(_multipart_field(body, "targets")) if b'name="targets"' in body else []
            )
            self.uploads.append(
                {
                    "metadata": json.loads(_multipart_field(body, "metadata")),
                    "targets": targets,
                    "body": body,
                }
            )
            return httpx.Response(
                201, json={"data": {"note_id": "note-run", "project_id": PROJECT_ID}}
            )
        return httpx.Response(500, json={"error": {"message": "unexpected request"}})

    def client(self) -> LabTracker:
        return LabTracker(base_url="http://testserver", transport=httpx.MockTransport(self.handler))


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for name in (
        "LAB_TRACKER_PROJECT_ID",
        "LAB_TRACKER_BASE_URL",
        "LAB_TRACKER_MCP_BASE_URL",
        "LAB_TRACKER_SESSION_ID",
        "LAB_TRACKER_SESSION_CONTEXT",
        "LAB_TRACKER_WATCH_CONFIG",
        "LAB_TRACKER_WATCH_OUTBOX",
        "LAB_TRACKER_WORKTREE_TREE",
        "LAB_TRACKER_GIT_TIMEOUT_SECONDS",
        "LAB_TRACKER_CONTAINER_REF",
    ):
        monkeypatch.delenv(name, raising=False)
    gitinfo._reset_worktree_tree_cache_for_tests()


@pytest.fixture
def bound_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "analysis"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "remote", "add", "origin", "https://sam:ghp_secretvalue@github.com/lab/analysis.git")
    (root / ".gitignore").write_text(".lab-tracker/\n", encoding="utf-8")
    (root / "lt_ids.json").write_text(json.dumps({"project_id": PROJECT_ID}), encoding="utf-8")
    (root / "analysis.py").write_text("print('v1')\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "results").mkdir()
    (root / "results" / "kept.csv").write_text("unchanged\n", encoding="utf-8")
    (root / "results" / "old.csv").write_text("old\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial analysis")
    monkeypatch.chdir(root)
    return root


def _only_event(root: Path) -> tuple[Path, dict]:
    events = sorted((root / ".lab-tracker" / "outbox" / "watch").glob("*.json"))
    assert len(events) == 1, events
    return events[0], read_event(events[0])


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


# --- redaction ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["tool", "--token", "s3cr3t", "--api-key=abc123", "--verbose"],
            ["tool", "--token", REDACTED, f"--api-key={REDACTED}", "--verbose"],
        ),
        (
            ["java", "-password", "hunter2", "-Xmx4g"],
            ["java", "-password", REDACTED, "-Xmx4g"],
        ),
        (
            ["env", "API_TOKEN=abc", "PATH=/usr/bin", "PGPASSWORD=pw", "python", "lr=0.1"],
            ["env", f"API_TOKEN={REDACTED}", "PATH=/usr/bin", f"PGPASSWORD={REDACTED}"]
            + ["python", "lr=0.1"],
        ),
        (
            ["python", "train.py", "db.password=hunter2", "+trainer.api_key=k", "epochs=3"],
            ["python", "train.py", f"db.password={REDACTED}", f"+trainer.api_key={REDACTED}"]
            + ["epochs=3"],
        ),
        (
            [
                "curl",
                "-H",
                "Authorization: Bearer abc.def.ghi",
                "https://u:pw@host/x?token=z&page=2",
            ],
            [
                "curl",
                "-H",
                f"Authorization: {REDACTED}",
                f"https://{REDACTED}@host/x?token={REDACTED}&page=2",
            ],
        ),
        (
            ["git", "clone", "https://ghp_abcdefghijklmnopqrstuvwxyz0123@github.com/lab/x.git"],
            ["git", "clone", f"https://{REDACTED}@github.com/lab/x.git"],
        ),
        (
            ["rsync", "ssh://git@host/repo", "ssh://git:pw@host/repo"],
            ["rsync", "ssh://git@host/repo", f"ssh://git:{REDACTED}@host/repo"],
        ),
        (
            ["deploy", "ghp_" + "a" * 36, "sk-ant-" + "b" * 30, "AKIAABCDEFGHIJKLMNOP"],
            ["deploy", REDACTED, REDACTED, REDACTED],
        ),
        (
            ["python", "fit.py", "--author", "Ada", "--no-password", "data.csv"]
            + ["--password-file", "pw.txt", "--tokenizer", "bert", "--token-name", "ci"],
            ["python", "fit.py", "--author", "Ada", "--no-password", "data.csv"]
            + ["--password-file", "pw.txt", "--tokenizer", "bert", "--token-name", "ci"],
        ),
        (["tool", "--secret"], ["tool", "--secret"]),
        (["tool", "--secret", "--next"], ["tool", "--secret", "--next"]),
    ],
)
def test_obvious_secrets_in_argv_are_redacted(argv: list[str], expected: list[str]) -> None:
    assert redact_argv(argv) == expected


# --- exit codes -----------------------------------------------------------------


def test_exit_codes_pass_through_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert run_command(_python("print('hello')"), RunOptions()) == 0
    assert run_command(_python("raise SystemExit(3)"), RunOptions()) == 3
    out, err = capfd.readouterr()
    # stdout is the command's own; the unbound notice is one stderr line per run.
    assert out == "hello\n"
    assert all("not capturing" in line for line in err.strip().splitlines())


def test_a_missing_executable_exits_127_like_the_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    code = run_command(["lt-run-no-such-command-xyz", "--flag"], RunOptions(project_id=PROJECT_ID))

    assert code == 127
    err = capfd.readouterr().err
    assert err.strip().splitlines() == ["lt run: lt-run-no-such-command-xyz: command not found"]
    assert not (tmp_path / ".lab-tracker").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX execute permission")
def test_a_file_that_is_not_executable_exits_126(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    script = tmp_path / "script.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o644)

    assert run_command([str(script)], RunOptions()) == 126


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_a_command_killed_by_a_signal_exits_128_plus_the_signal(
    bound_repo: Path,
) -> None:
    code = run_command(
        _python("import os, signal; os.kill(os.getpid(), signal.SIGTERM)"),
        RunOptions(drain=False),
    )

    assert code == 128 + 15
    _path, event = _only_event(bound_repo)
    metadata = event["payload"]["metadata"]
    assert metadata["run_exit_code"] == 143
    assert metadata["run_exit_signal"] == 15
    assert "terminated by signal 15" in event["payload"]["body"]


def test_the_cli_exits_with_the_commands_code_and_prints_nothing_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exited:
        lt_cli.main(["run", "--no-drain", "--", *_python("print('out'); raise SystemExit(7)")])

    assert exited.value.code == 7
    out, err = capfd.readouterr()
    assert out == "out\n"
    assert len(err.strip().splitlines()) == 1


def test_the_cli_requires_a_command(capfd: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        lt_cli.main(["run", "--label", "nothing", "--"])

    assert exited.value.code == 2
    assert "a command is required" in capfd.readouterr().err


# --- capture --------------------------------------------------------------------


def test_a_bound_run_writes_one_staged_note_event_with_outputs_and_code_identity(
    bound_repo: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    head_tree = _git(bound_repo, "rev-parse", "HEAD^{tree}")
    head = _git(bound_repo, "rev-parse", "HEAD")
    script = (
        "from pathlib import Path\n"
        "Path('results/new.csv').write_text('a,b\\n1,2\\n')\n"
        "Path('results/old.csv').write_text('changed\\n')\n"
        "Path('results/sub').mkdir()\n"
        "Path('results/sub/plot.png').write_bytes(b'png')\n"
    )

    code = run_command(
        [*_python(script), "--token", "s3cr3t"],
        RunOptions(
            question_id=QUESTION_ID,
            session=SESSION_ID,
            label="fit decoder",
            outputs=("results",),
            drain=False,
            request_draft=True,
        ),
    )

    assert code == 0
    assert capfd.readouterr().err == ""
    path, event = _only_event(bound_repo)
    assert "s3cr3t" not in path.read_text(encoding="utf-8")
    assert "ghp_secretvalue" not in path.read_text(encoding="utf-8")
    assert event["capture_kind"] == "command_run"
    assert event["adapter"] == "lt-run"
    assert event["sink"] == "staged-note"
    assert event["context"] == {
        "project_id": PROJECT_ID,
        "question_id": QUESTION_ID,
        "dataset_ids": [],
        "tags": [],
        "session_id": SESSION_ID,
    }
    assert event["source"]["provider"] == "lt-run"
    assert event["source"]["session_source"] == "explicit"
    payload = event["payload"]
    assert payload["status"] == "staged"
    assert payload["request_draft"] is True
    assert payload["title"] == "lt run: fit decoder"

    artifacts = {artifact["title"]: artifact for artifact in event["artifacts"]}
    assert sorted(artifacts) == ["results/new.csv", "results/old.csv", "results/sub/plot.png"]
    new_csv = artifacts["results/new.csv"]
    assert new_csv["summary"] == "created"
    assert new_csv["kind"] == "file"
    assert new_csv["uri"] == (bound_repo / "results" / "new.csv").as_uri()
    assert new_csv["size_bytes"] == len("a,b\n1,2\n")
    assert new_csv["content_hash"].startswith("sha256:")
    assert new_csv["modified_at"].endswith("+00:00")
    assert artifacts["results/old.csv"]["summary"] == "modified"

    metadata = payload["metadata"]
    assert metadata["run_exit_code"] == 0
    assert metadata["run_label"] == "fit decoder"
    assert metadata["run_git_commit"] == head
    assert metadata["run_git_dirty"] is False
    # The outputs folder and .lab-tracker/ are not code: the tree is HEAD's.
    assert metadata["run_git_worktree_tree"] == head_tree
    assert metadata["run_repo_remote_url"] == "github.com/lab/analysis"
    assert metadata["run_environment_hash"].startswith("sha256:")
    assert metadata["run_environment_files"] == "uv.lock"
    assert metadata["run_output_count"] == 3
    assert metadata["run_output_roots"] == "results"
    assert metadata["run_cwd"] == str(bound_repo.resolve())
    assert metadata["run_duration_seconds"] >= 0
    assert f"--token {REDACTED}" in str(metadata["run_command"])
    assert "run_outputs_truncated" not in metadata

    body = payload["body"]
    assert body.startswith("# lt run: fit decoder\n")
    assert f"--token {REDACTED}" in body
    assert f"`{head_tree}`" in body
    assert "`results/new.csv` — created" in body


def test_the_synced_note_carries_the_run_metadata_and_declared_targets(
    bound_repo: Path,
) -> None:
    run_command(
        _python("print('ok')"),
        RunOptions(question_id=QUESTION_ID, drain=False),
    )
    server = _Server()

    with server.client() as lt:
        summary = sync_outbox_path(lt, bound_repo / ".lab-tracker" / "outbox" / "watch")

    assert summary["errors"] == []
    (upload,) = server.uploads
    metadata = upload["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["run_git_worktree_tree"] == _git(bound_repo, "rev-parse", "HEAD^{tree}")
    assert metadata["run_exit_code"] == 0
    assert metadata["watch_capture_kind"] == "command_run"
    assert metadata["watch_adapter"] == "lt-run"
    assert metadata["evidence_source_provider"] == "lt-run"
    assert metadata["declared_target_source"] == "explicit"
    assert upload["targets"] == [{"entity_type": "question", "entity_id": QUESTION_ID}]
    assert b"# lt run:" in upload["body"]  # type: ignore[operator]


def test_large_outputs_get_size_and_mtime_but_no_hash_and_the_list_is_bounded(
    bound_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_capture, "MAX_HASH_FILE_BYTES", 10)
    monkeypatch.setattr(run_capture, "MAX_OUTPUT_ARTIFACTS", 2)
    script = (
        "from pathlib import Path\n"
        "Path('results/a_big.bin').write_bytes(b'x' * 64)\n"
        "Path('results/b_small.txt').write_text('tiny')\n"
        "Path('results/c_more.txt').write_text('more')\n"
    )

    run_command(_python(script), RunOptions(outputs=("results",), drain=False))

    _path, event = _only_event(bound_repo)
    big, small = event["artifacts"]
    assert big["title"] == "results/a_big.bin"
    assert "content_hash" not in big
    assert big["size_bytes"] == 64
    assert big["modified_at"]
    assert small["content_hash"].startswith("sha256:")
    metadata = event["payload"]["metadata"]
    assert metadata["run_output_count"] == 3
    assert metadata["run_outputs_truncated"] is True
    assert "not hashed" in event["payload"]["body"]


def test_output_snapshots_diff_created_modified_and_removed_files(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    (root / "same.txt").write_text("same", encoding="utf-8")
    (root / "grows.txt").write_text("a", encoding="utf-8")
    (root / "gone.txt").write_text("bye", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    before = run_capture.snapshot_outputs([root])

    (root / "grows.txt").write_text("abc", encoding="utf-8")
    (root / "gone.txt").unlink()
    (root / "fresh.txt").write_text("new", encoding="utf-8")
    (root / ".git" / "HEAD").write_text("changed", encoding="utf-8")
    after = run_capture.snapshot_outputs([root])
    changes = run_capture.diff_snapshots(before, after)

    assert changes.created == [root / "fresh.txt"]
    assert changes.modified == [root / "grows.txt"]
    assert changes.removed == 1
    assert run_capture.snapshot_outputs([root / "missing"]).files == {}


def test_no_output_folders_are_watched_unless_declared(bound_repo: Path) -> None:
    run_command(_python("open('results/new.csv', 'w').write('x')"), RunOptions(drain=False))

    _path, event = _only_event(bound_repo)
    assert event["artifacts"] == []
    assert event["payload"]["metadata"]["run_output_count"] == 0


def test_an_unbound_checkout_runs_the_command_and_captures_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "unbound"
    root.mkdir()
    _git(root, "init", "-q")
    monkeypatch.chdir(root)

    code = run_command(_python("raise SystemExit(4)"), RunOptions())

    assert code == 4
    (line,) = capfd.readouterr().err.strip().splitlines()
    assert "not capturing" in line and "lt project bind" in line
    assert not (root / ".lab-tracker").exists()


def test_capture_failures_never_change_the_exit_code_and_print_one_line(
    bound_repo: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    def explode(*_args: object, **_kwargs: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(run_capture.watch_capture, "write_event", explode)

    code = run_command(_python("raise SystemExit(5)"), RunOptions(drain=False))

    assert code == 5
    (line,) = capfd.readouterr().err.strip().splitlines()
    assert "could not record this run" in line and "disk full" in line


def test_a_bad_session_reference_is_dropped_but_the_run_is_still_captured(
    bound_repo: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    code = run_command(_python("print('ran')"), RunOptions(session="LT-NOTACODE", drain=False))

    assert code == 0
    out, err = capfd.readouterr()
    assert out == "ran\n"
    (line,) = err.strip().splitlines()
    assert "--session" in line
    _path, event = _only_event(bound_repo)
    assert event["context"]["session_id"] is None


def test_the_active_session_rides_along_for_the_sync_to_verify(
    bound_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = bound_repo / ".lab-tracker" / "session.json"
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text(
        json.dumps(
            {
                "session_id": SESSION_ID,
                "project_id": PROJECT_ID,
                "expires_at": "2999-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    run_command(_python("pass"), RunOptions(drain=False))

    _path, event = _only_event(bound_repo)
    assert event["context"]["session_id"] == SESSION_ID
    assert event["source"]["session_source"] == "active"
    assert event["source"]["session_project_id"] == PROJECT_ID
    # A gitignored .lab-tracker/ that exists (the usual configured checkout)
    # never costs the run its code identity.
    head_tree = _git(bound_repo, "rev-parse", "HEAD^{tree}")
    assert event["payload"]["metadata"]["run_git_worktree_tree"] == head_tree


# --- drain ----------------------------------------------------------------------


def test_a_configured_server_drains_the_run_right_away(
    bound_repo: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    server = _Server()

    code = run_command(_python("pass"), RunOptions(), client_factory=server.client)

    assert code == 0
    assert capfd.readouterr().err == ""
    assert len(server.uploads) == 1
    _path, event = _only_event(bound_repo)
    assert event["sync"]["status"] == "synced"


def test_an_unreachable_server_leaves_the_run_queued_with_one_notice(
    bound_repo: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    def offline() -> LabTracker:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        return LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler))

    code = run_command(_python("raise SystemExit(9)"), RunOptions(), client_factory=offline)

    assert code == 9
    (line,) = capfd.readouterr().err.strip().splitlines()
    assert "lt outbox sync" in line
    _path, event = _only_event(bound_repo)
    assert event["sync"]["status"] != "synced"


def test_a_black_holed_server_costs_one_short_health_probe(
    bound_repo: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[str, object]] = []

    def black_holed() -> LabTracker:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, request.extensions.get("timeout")))
            raise httpx.ReadTimeout("no answer", request=request)

        return LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler))

    code = run_command(_python("raise SystemExit(4)"), RunOptions(), client_factory=black_holed)

    assert code == 4
    # One probe with the short timeout, then nothing: no per-event timeouts.
    assert [path for path, _timeout in seen] == ["/health"]
    timeout = seen[0][1]
    assert isinstance(timeout, dict)
    assert timeout["read"] == run_capture.HEALTH_PROBE_TIMEOUT_SECONDS
    (line,) = capfd.readouterr().err.strip().splitlines()
    assert "/health" in line and "lt outbox sync" in line
    _path, event = _only_event(bound_repo)
    assert event["sync"]["status"] == "pending"


def test_the_post_run_drain_syncs_a_bounded_number_of_events(
    bound_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lab_tracker_client import watch as watch_capture

    outbox = bound_repo / ".lab-tracker" / "outbox" / "watch"
    backlog = watch_capture.make_event(
        capture_id="aaa-earlier-capture",
        capture_kind="note",
        adapter="test",
        sink=watch_capture.SINK_STAGED_NOTE,
        context={"project_id": PROJECT_ID},
        payload={"title": "backlog", "body": "# queued earlier\n", "status": "staged"},
    )
    watch_capture.write_event(backlog, outbox)
    monkeypatch.setattr(run_capture, "DRAIN_LIMIT", 1)
    server = _Server()

    assert run_command(_python("pass"), RunOptions(), client_factory=server.client) == 0

    assert server.paths[0] == "/health"
    assert len(server.uploads) == 1
    statuses = sorted(read_event(path)["sync"]["status"] for path in outbox.glob("*.json"))
    assert statuses == ["pending", "synced"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_an_interrupt_sent_to_lt_run_reaches_the_command(tmp_path: Path) -> None:
    """``kill -INT <lt pid>`` (or a notebook kernel interrupt) must reach the
    command: no terminal delivered it to the command's process group."""

    import signal
    import time

    ready = tmp_path / "ready"
    child = f"import pathlib, time; pathlib.Path({str(ready)!r}).touch(); time.sleep(60)"
    env = {key: value for key, value in os.environ.items() if not key.startswith("LAB_TRACKER_")}
    process = subprocess.Popen(
        [sys.executable, "-m", "lab_tracker_client", "run", "--no-drain", "--"]
        + [sys.executable, "-c", child],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not ready.exists():
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "the command never started"
            time.sleep(0.05)
        process.send_signal(signal.SIGINT)
        _out, err = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()

    assert process.returncode == 130, err
    assert "KeyboardInterrupt" in err.decode("utf-8", "replace")


def test_interrupts_are_left_to_the_terminal_only_in_its_foreground_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_capture.os, "isatty", lambda _fd: False)
    assert run_capture._terminal_delivers_interrupts() is False

    monkeypatch.setattr(run_capture.os, "isatty", lambda _fd: True)
    monkeypatch.setattr(run_capture.os, "getpgrp", lambda: 4242, raising=False)
    monkeypatch.setattr(run_capture.os, "tcgetpgrp", lambda _fd: 4242, raising=False)
    assert run_capture._terminal_delivers_interrupts() is True

    monkeypatch.setattr(run_capture.os, "tcgetpgrp", lambda _fd: 7, raising=False)
    assert run_capture._terminal_delivers_interrupts() is False


# --- pre-exec git cost ----------------------------------------------------------


def test_one_read_only_status_serves_the_dirty_flag_and_the_tree(
    bound_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (bound_repo / "analysis.py").write_text("print('v2 uncommitted')\n", encoding="utf-8")

    def second_status(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the dirty flag must reuse the worktree's git status")

    monkeypatch.setattr(run_capture, "git_dirty_state", second_status)

    assert run_command(_python("pass"), RunOptions(drain=False)) == 0

    _path, event = _only_event(bound_repo)
    metadata = event["payload"]["metadata"]
    assert metadata["run_git_dirty"] is True
    assert metadata["run_git_worktree_tree"] != _git(bound_repo, "rev-parse", "HEAD^{tree}")


def test_lt_run_never_refreshes_the_real_index(bound_repo: Path) -> None:
    # A tracked file with a new mtime but the same bytes: a plain `git status`
    # re-hashes it and rewrites the index with fresh stat data.
    tracked = bound_repo / "analysis.py"
    stat = tracked.stat()
    os.utime(tracked, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    index = bound_repo / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    assert run_command(_python("pass"), RunOptions(drain=False)) == 0

    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
    _git(bound_repo, "status", "--porcelain")
    # The scenario was real: an ordinary status does rewrite it.
    assert (index.read_bytes(), index.stat().st_mtime_ns) != before


def test_no_server_configured_means_no_network_call(
    bound_repo: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(bound_repo.parent / "no-profile"))

    def forbidden(*_args: object, **_kwargs: object) -> LabTracker:
        raise AssertionError("no client may be built when no server is configured")

    monkeypatch.setattr(run_capture.LabTracker, "from_env", forbidden)

    assert run_command(_python("pass"), RunOptions()) == 0
    assert capfd.readouterr().err == ""
    _path, event = _only_event(bound_repo)
    assert event["sync"]["status"] == "pending"


def test_no_drain_queues_without_touching_the_network(bound_repo: Path) -> None:
    def forbidden() -> LabTracker:
        raise AssertionError("--no-drain must not build a client")

    assert run_command(_python("pass"), RunOptions(drain=False), client_factory=forbidden) == 0
    _path, event = _only_event(bound_repo)
    assert event["sync"]["status"] == "pending"


def test_each_run_is_its_own_event(bound_repo: Path) -> None:
    run_command(_python("pass"), RunOptions(drain=False))
    run_command(_python("pass"), RunOptions(drain=False))

    events = sorted((bound_repo / ".lab-tracker" / "outbox" / "watch").glob("*.json"))
    run_ids = {read_event(path)["payload"]["metadata"]["run_id"] for path in events}
    assert len(events) == 2 and len(run_ids) == 2
    assert all(uuid.UUID(read_event(path)["event_id"]) for path in events)
