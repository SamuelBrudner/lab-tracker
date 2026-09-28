from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

from lab_tracker_client import LabTracker, pipeline_capture
from lab_tracker_client import cli as lt_cli
from lab_tracker_client.pipeline_capture import (
    PipelineRun,
    artifact_pointer,
    capture_pipeline_run,
    expand_path_args,
    report_pipeline_run,
)
from lab_tracker_client.redaction import redact_capture_text
from lab_tracker_client.session_context import encode_session_link_code

_ENV = (
    "LAB_TRACKER_BASE_URL",
    "LAB_TRACKER_MCP_BASE_URL",
    "LAB_TRACKER_PROJECT_ID",
    "LAB_TRACKER_SESSION_ID",
    "LAB_TRACKER_SESSION_CONTEXT",
    "LAB_TRACKER_WATCH_CONFIG",
    "LAB_TRACKER_WATCH_OUTBOX",
    "LAB_TRACKER_PIPELINE_CAPTURE",
    "LAB_TRACKER_ACCESS_TOKEN",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    pipeline_capture._reset_notices_for_tests()
    yield
    pipeline_capture._reset_notices_for_tests()


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(path: Path, remote: str = "https://user:s3cret@github.com/Lab/Analysis.git") -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "remote", "add", "origin", remote)
    (path / ".gitignore").write_text(".lab-tracker/\nresults/\ndata/\nlogs/\n", encoding="utf-8")
    (path / "Snakefile").write_text("rule all:\n", encoding="utf-8")
    _git(path, "add", ".gitignore", "Snakefile")
    _git(path, "commit", "-q", "-m", "pipeline")
    return _git(path, "rev-parse", "HEAD")


def _events(checkout: Path) -> list[dict]:
    outbox = checkout / ".lab-tracker" / "outbox" / "watch"
    return [json.loads(path.read_text()) for path in sorted(outbox.glob("*.json"))]


def test_report_writes_one_staged_note_event_with_bounded_pointers(tmp_path) -> None:
    commit = _repo(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    (data / "raw.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (data / "huge.bin").write_bytes(b"x" * 64)
    results = tmp_path / "results"
    (results / "plots").mkdir(parents=True)
    (results / "table.csv").write_text("x\n1\n", encoding="utf-8")
    (results / "plots" / "a.png").write_bytes(b"png")
    log = tmp_path / "logs" / "run.log"
    log.parent.mkdir()
    log.write_text(
        "start\n" + "noise\n" * 2000 + "Authorization: Bearer abc.def\n--token hunter2 done\n",
        encoding="utf-8",
    )

    payload = report_pipeline_run(
        PipelineRun(
            engine="snakemake",
            status="success",
            run_id="run-1",
            started_at="2026-09-01T10:00:00+02:00",
            inputs=[
                "data/raw.csv",
                "data/huge.bin",
                "s3://user:pw@bucket/raw?sig=x",
                "missing.txt",
            ],
            outputs=["results/table.csv", "results/plots"],
            logs=[log],
            label="nightly",
            metadata={"snakemake_jobs_finished": 3, "pipeline_custom": "ok"},
        ),
        cwd=tmp_path,
        project_id="project-1",
        question_id="question-1",
        hash_max_bytes=32,
        drain=False,
    )

    assert payload["action"] == "captured"
    assert payload["sync"] == "skipped"
    [event] = _events(tmp_path)
    assert event["capture_kind"] == "pipeline_run"
    assert event["adapter"] == "lt-pipeline"
    assert event["sink"] == "staged-note"
    assert event["context"]["project_id"] == "project-1"
    assert event["context"]["question_id"] == "question-1"
    source = event["source"]
    assert source["git_commit"] == commit
    assert source["git_dirty"] is False
    assert source["repo_remote_url"] == "https://github.com/Lab/Analysis.git"
    assert source["external_id"] == "pipeline:snakemake:github.com/lab/analysis:run-1"
    assert "path" not in source  # a path would make the watch sync upload that file
    pointers = {item["title"]: item for item in event["artifacts"]}
    raw = pointers["data/raw.csv"]
    assert raw["role"] == "input"
    assert raw["content_hash"].startswith("sha256:")
    assert raw["size_bytes"] == 8
    huge = pointers["data/huge.bin"]
    assert "content_hash" not in huge
    assert huge["size_bytes"] == 64
    assert "hashing cap" in huge["summary"]
    remote = pointers["s3://bucket/raw"]
    assert remote["kind"] == "remote"
    assert "pw" not in json.dumps(remote)
    assert "Missing" in pointers["missing.txt"]["summary"]
    assert pointers["results/table.csv"]["role"] == "output"
    plots = pointers["results/plots"]
    assert plots["kind"] == "directory"
    assert plots["file_count"] == 1
    assert "content_hash" not in plots
    metadata = event["payload"]["metadata"]
    assert metadata["pipeline_engine"] == "snakemake"
    assert metadata["pipeline_status"] == "success"
    assert metadata["pipeline_run_id"] == "run-1"
    assert metadata["pipeline_input_count"] == 4
    assert metadata["pipeline_output_count"] == 2
    assert metadata["pipeline_started_at"] == "2026-09-01T08:00:00+00:00"
    assert metadata["pipeline_label"] == "nightly"
    assert metadata["pipeline_snakemake_jobs_finished"] == 3
    assert metadata["pipeline_custom"] == "ok"
    assert metadata["pipeline_project_source"] == "explicit"
    assert metadata["pipeline_git_remote"] == "https://github.com/Lab/Analysis.git"
    assert all(isinstance(value, (str, int, float, bool)) for value in metadata.values())
    body = event["payload"]["body"]
    assert body.startswith("# Snakemake run nightly (run-1): success")
    assert "## Declared inputs (4)" in body
    assert "## Declared outputs (2)" in body
    assert f"- Git commit: `{commit}`" in body
    excerpt = event["log_excerpt"]
    assert len(excerpt) < pipeline_capture.LOG_EXCERPT_MAX_CHARS + 200
    assert "abc.def" not in excerpt and "hunter2" not in excerpt
    assert "abc.def" not in body and "s3cret" not in json.dumps(event)


def test_artifact_count_is_bounded_with_a_more_summary(tmp_path) -> None:
    _repo(tmp_path)
    outputs = [f"results/{index}.txt" for index in range(30)]
    payload = report_pipeline_run(
        PipelineRun(engine="generic", status="success", run_id="r", outputs=outputs),
        cwd=tmp_path,
        project_id="project-1",
        max_artifacts=5,
        drain=False,
    )

    [event] = _events(tmp_path)
    assert payload["output_count"] == 30
    assert len(event["artifacts"]) == 5
    assert event["payload"]["metadata"]["pipeline_outputs_omitted"] == 25
    assert "… and 25 more outputs" in event["payload"]["body"]


def test_hashing_budget_leaves_later_files_as_pointers(tmp_path, monkeypatch) -> None:
    _repo(tmp_path)
    for name in ("a", "b"):
        (tmp_path / f"{name}.bin").write_bytes(b"z" * 10)
    monkeypatch.setattr(pipeline_capture, "HASH_BUDGET_BYTES", 15)
    budget = pipeline_capture._HashBudget(15)

    first = artifact_pointer("a.bin", role="output", base=tmp_path, root=tmp_path, budget=budget)
    second = artifact_pointer("b.bin", role="output", base=tmp_path, root=tmp_path, budget=budget)

    assert first["content_hash"].startswith("sha256:")
    assert "content_hash" not in second
    assert "budget" in second["summary"]


def test_at_file_lists_expand(tmp_path) -> None:
    listing = tmp_path / "outputs.txt"
    listing.write_text("# produced files\nresults/a.csv\n\n  results/b.csv  \n", encoding="utf-8")

    assert expand_path_args(["x.csv", f"@{listing.name}"], base=tmp_path) == [
        "x.csv",
        "results/a.csv",
        "results/b.csv",
    ]


def test_same_run_reported_twice_is_one_event(tmp_path) -> None:
    _repo(tmp_path)
    run = PipelineRun(engine="nextflow", status="success", run_id="nf-7")

    first = report_pipeline_run(run, cwd=tmp_path, project_id="p", drain=False)
    second = report_pipeline_run(run, cwd=tmp_path, project_id="p", drain=False)

    assert first["action"] == "captured"
    assert second["action"] == "already_captured"
    assert len(_events(tmp_path)) == 1


def test_unbound_project_skips_with_one_notice_and_writes_nothing(tmp_path, capsys) -> None:
    _repo(tmp_path)
    run = PipelineRun(engine="generic", status="success", run_id="r")

    first = report_pipeline_run(run, cwd=tmp_path)
    second = report_pipeline_run(run, cwd=tmp_path)

    assert first["action"] == second["action"] == "skipped"
    assert first["reason"] == "project_unbound"
    assert not (tmp_path / ".lab-tracker").exists()
    assert capsys.readouterr().err.count("no project is bound") == 1


def test_project_resolves_from_the_checkout_binding(tmp_path, monkeypatch) -> None:
    _repo(tmp_path)
    (tmp_path / "lt_ids.json").write_text(json.dumps({"project_id": "bound-1"}), encoding="utf-8")
    subdir = tmp_path / "workflow"
    subdir.mkdir()

    payload = report_pipeline_run(PipelineRun(run_id="r"), cwd=subdir, drain=False)

    assert payload["project_id"] == "bound-1"
    assert payload["project_source"] == "checkout"
    monkeypatch.setenv("LAB_TRACKER_PROJECT_ID", "env-1")
    assert (
        report_pipeline_run(PipelineRun(run_id="r2"), cwd=subdir, drain=False)["project_source"]
        == "environment"
    )


def test_project_resolves_from_an_hpc_config_outside_git(tmp_path) -> None:
    (tmp_path / ".lab-tracker").mkdir()
    (tmp_path / ".lab-tracker" / "hpc.json").write_text(
        json.dumps({"project_id": "hpc-project"}), encoding="utf-8"
    )

    payload = report_pipeline_run(PipelineRun(run_id="r"), cwd=tmp_path, drain=False)

    [event] = _events(tmp_path)
    assert payload["project_source"] == "hpc_config"
    assert event["context"]["project_id"] == "hpc-project"
    assert "git_commit" not in event["source"]


def test_session_link_code_becomes_the_declared_session(tmp_path) -> None:
    _repo(tmp_path)
    session_id = str(uuid.uuid4())

    report_pipeline_run(
        PipelineRun(run_id="r"),
        cwd=tmp_path,
        project_id="p",
        session=f"LT-{encode_session_link_code(session_id)}",
        drain=False,
    )

    [event] = _events(tmp_path)
    assert event["context"]["session_id"] == session_id
    assert event["source"]["session_source"] == "config"


def test_no_network_call_when_no_server_is_configured(tmp_path, monkeypatch) -> None:
    _repo(tmp_path)

    def _boom() -> LabTracker:
        raise AssertionError("no client may be built when no server is configured")

    monkeypatch.setattr(pipeline_capture, "_default_client", _boom)

    payload = report_pipeline_run(PipelineRun(run_id="r"), cwd=tmp_path, project_id="p")

    assert payload["sync"] == "not_configured"
    assert payload["action"] == "captured"


def test_drain_uploads_the_staged_note_with_pipeline_metadata(tmp_path, monkeypatch) -> None:
    commit = _repo(tmp_path)
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://testserver")
    uploads: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "GET" and request.url.path == "/notes":
            return httpx.Response(200, json={"data": [], "meta": {"total": 0}})
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            uploads.append(request.content)
            return httpx.Response(
                201, json={"data": {"note_id": "note-1", "project_id": "p", "status": "staged"}}
            )
        if request.url.path == "/notes/note-1/analysis-graph-drafts":
            return httpx.Response(201, json={"data": {"change_set_id": "cs-1"}})
        return httpx.Response(500, json={"error": {"message": "unexpected"}})

    monkeypatch.setattr(
        pipeline_capture,
        "_default_client",
        lambda: LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)),
    )

    payload = report_pipeline_run(
        PipelineRun(engine="kedro", status="error", run_id="k-1", error_text="ValueError: bad"),
        cwd=tmp_path,
        project_id="p",
        request_draft=True,
    )

    assert payload["sync"]["errors"] == []
    assert "sync_error" not in payload
    [event] = _events(tmp_path)
    assert event["sync"]["status"] == "synced"
    assert event["sync"]["change_set_id"] == "cs-1"
    [upload] = uploads
    assert b"# Kedro run k-1: error" in upload
    assert b"pipeline_engine" in upload
    assert commit.encode() in upload
    assert b"ValueError: bad" in upload


def test_drain_failure_keeps_the_event_queued_with_one_notice(tmp_path, monkeypatch, capsys):
    _repo(tmp_path)
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://testserver")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        pipeline_capture,
        "_default_client",
        lambda: LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)),
    )

    payload = report_pipeline_run(PipelineRun(run_id="r"), cwd=tmp_path, project_id="p")

    assert payload["action"] == "captured"
    assert payload["sync_error"]
    [event] = _events(tmp_path)
    assert event["sync"]["status"] != "synced"
    err = capsys.readouterr().err
    assert err.count("is queued in") == 1


def test_kill_switch_disables_capture(tmp_path, monkeypatch) -> None:
    _repo(tmp_path)
    monkeypatch.setenv("LAB_TRACKER_PIPELINE_CAPTURE", "off")

    payload = report_pipeline_run(PipelineRun(run_id="r"), cwd=tmp_path, project_id="p")

    assert payload["action"] == "disabled"
    assert not (tmp_path / ".lab-tracker").exists()


def test_capture_pipeline_run_never_raises(tmp_path, capsys) -> None:
    payload = capture_pipeline_run(PipelineRun(engine="airflow"), cwd=tmp_path, project_id="p")

    assert payload["action"] == "failed"
    assert "engine must be one of" in payload["error"]
    assert capsys.readouterr().err.count("pipeline run not recorded") == 1


def test_redaction_scrubs_common_credential_shapes(monkeypatch) -> None:
    monkeypatch.setenv("LAB_TRACKER_ACCESS_TOKEN", "lpat_supersecretvalue")
    text = (
        "token lpat_supersecretvalue\n"
        "password=hunter2 api_key: 'abc123'\n"
        "curl https://bob:pw@example.org/x --api-key zzz\n"
        "ghp_" + "a" * 36 + " Bearer eyJhbGciOi\n"
        "max_tokens=100 tokens: 5"
    )

    cleaned = redact_capture_text(text)

    for secret in ("supersecretvalue", "hunter2", "abc123", "bob:pw", "zzz", "ghp_a", "eyJhbG"):
        assert secret not in cleaned
    assert "max_tokens=100" in cleaned and "tokens: 5" in cleaned


def test_cli_report_prints_json_and_fail_silent_swallows_errors(tmp_path, capsys) -> None:
    checkout = tmp_path / "repo"
    _repo(checkout)
    (checkout / "results").mkdir()
    (checkout / "results" / "out.csv").write_text("1\n", encoding="utf-8")
    listing = checkout / "outs.txt"
    listing.write_text("results/out.csv\n", encoding="utf-8")

    lt_cli.main(
        [
            "pipeline",
            "report",
            "--engine",
            "generic",
            "--status",
            "success",
            "--output",
            f"@{listing}",
            "--project",
            "p",
            "--run-id",
            "cli-1",
            "--cwd",
            str(checkout),
            "--no-drain",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "pipeline-report"
    assert payload["output_count"] == 1

    blocker = tmp_path / "blocked"
    blocker.mkdir()
    (blocker / ".lab-tracker").write_text("not a directory", encoding="utf-8")
    args = ["pipeline", "report", "--project", "p", "--cwd", str(blocker), "--no-drain"]
    with pytest.raises(SystemExit) as raised:
        lt_cli.main(args)
    assert "lt pipeline report:" in str(raised.value.code)
    capsys.readouterr()

    lt_cli.main([*args, "--fail-silent"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def _tail_excerpt(tmp_path: Path, prefix: str, secret: str) -> str:
    """A log whose last LOG_EXCERPT_MAX_CHARS characters start inside ``prefix``."""

    limit = pipeline_capture.LOG_EXCERPT_MAX_CHARS
    secret_line = f"{prefix}{secret}\n"
    end = "\nrun finished\n"
    # Size the tail so the cut lands after the prefix's first two characters,
    # e.g. "Be|arer <secret>" or "gh|p_<secret>".
    fill = limit - (len(secret_line) - 2) - len(end)
    tail = secret_line + "z" * fill + end
    log = tmp_path / f"cut-{len(prefix)}.log"
    log.write_text("start\n" + "filler line\n" * 600 + tail, encoding="utf-8")
    old_style_cut = log.read_text(encoding="utf-8")[-limit:]
    assert old_style_cut.startswith(prefix[2:])  # the fixture really splits the prefix
    return pipeline_capture._log_excerpt([log], None, base=tmp_path)


@pytest.mark.parametrize(
    ("prefix", "secret"),
    [("Bearer ", "eyJhbGciOiJIUzI1NiJ9.cGF5bG9hZA.c2lnbmF0dXJl"), ("ghp_", "A1b2" * 9)],
)
def test_log_tail_is_redacted_before_it_is_cut(tmp_path, prefix, secret) -> None:
    excerpt = _tail_excerpt(tmp_path, prefix, secret)

    assert secret not in excerpt
    assert secret[:12] not in excerpt
    assert "run finished" in excerpt
    assert len(excerpt) <= pipeline_capture.LOG_EXCERPT_MAX_CHARS + 100


def test_error_text_is_redacted_before_it_is_cut(tmp_path) -> None:
    head = "x" * (pipeline_capture.ERROR_TEXT_MAX_CHARS - 10)
    excerpt = pipeline_capture._log_excerpt(
        [], head + " password=hunter2hunter2 trailing", base=tmp_path
    )

    assert "hunter2" not in excerpt


def _black_hole_client(requests: list[str], *, health_ok: bool) -> LabTracker:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path == "/health" and health_ok:
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "GET" and request.url.path == "/notes" and health_ok:
            return httpx.Response(200, json={"data": [], "meta": {"total": 0}})
        raise httpx.ConnectTimeout("timed out", request=request)

    return LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler))


def _queue_runs(checkout: Path, count: int) -> None:
    for index in range(count):
        report_pipeline_run(
            PipelineRun(run_id=f"queued-{index}"), cwd=checkout, project_id="p", drain=False
        )


def test_drain_probes_health_once_and_skips_a_dead_server(tmp_path, monkeypatch) -> None:
    _repo(tmp_path)
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://testserver")
    _queue_runs(tmp_path, 5)
    requests: list[str] = []
    monkeypatch.setattr(
        pipeline_capture, "_default_client", lambda: _black_hole_client(requests, health_ok=False)
    )

    payload = report_pipeline_run(PipelineRun(run_id="r"), cwd=tmp_path, project_id="p")

    assert requests == ["GET /health"]
    assert payload["action"] == "captured"
    assert payload["sync_error"]
    assert all(event["sync"]["status"] == "pending" for event in _events(tmp_path))


def test_drain_stops_at_the_first_connection_failure(tmp_path, monkeypatch) -> None:
    _repo(tmp_path)
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://testserver")
    _queue_runs(tmp_path, 5)
    requests: list[str] = []
    monkeypatch.setattr(
        pipeline_capture, "_default_client", lambda: _black_hole_client(requests, health_ok=True)
    )

    payload = report_pipeline_run(PipelineRun(run_id="r"), cwd=tmp_path, project_id="p")

    assert requests.count("POST /notes/upload-file") == 1
    assert payload["sync_error"]
    events = _events(tmp_path)
    assert len(events) == 6
    assert not any(event["sync"]["status"] == "synced" for event in events)
