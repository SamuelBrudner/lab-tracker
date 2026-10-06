"""`lt capture file`: the language-neutral single-file capture entry point.

R, MATLAB and shell pipelines shell out to it, so its contract is tested as a
contract: the FigureCaptureResult dict as JSON on stdout, exit 0 for every
fail-soft outcome (imported, coalesced, queued, skipped, failed), and a
nonzero exit only for a usage error.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

import lab_tracker_client.figure as figure_module
from lab_tracker_client import LabTracker, cli_capture
from lab_tracker_client import cli as lt_cli
from lab_tracker_client.figure import _reset_figure_capture_state_for_tests

Handler = Callable[[httpx.Request], httpx.Response]
FIGURE_RESULT_KEYS = {
    "action",
    "path",
    "source_external_id",
    "source_uri",
    "evidence_content_hash",
    "metadata",
    "client_capture_id",
    "no_preview",
    "stale_review_bytes",
}


def _multipart_field(body: bytes, name: str) -> str:
    marker = f'name="{name}"'.encode()
    assert marker in body
    chunk = body.split(marker, 1)[1].split(b"\r\n\r\n", 1)[1]
    return chunk.split(b"\r\n--", 1)[0].decode("utf-8")


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _reset_figure_capture_state_for_tests()
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-config"))
    monkeypatch.setenv("LAB_TRACKER_WATCH_OUTBOX", str(tmp_path / "outbox"))
    for key in (
        "LAB_TRACKER_PROJECT_ID",
        "LAB_TRACKER_BASE_URL",
        "LAB_TRACKER_MCP_BASE_URL",
        "LAB_TRACKER_ACCESS_TOKEN",
        "LAB_TRACKER_USERNAME",
        "LAB_TRACKER_PASSWORD",
        "LAB_TRACKER_MCP_USERNAME",
        "LAB_TRACKER_MCP_PASSWORD",
        "LAB_TRACKER_SESSION_ID",
        "LAB_TRACKER_SESSION_CONTEXT",
        "LAB_TRACKER_WATCH_CONFIG",
        "LAB_TRACKER_CAPTURE_OUTBOX",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    yield
    _reset_figure_capture_state_for_tests()


def _serve(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[httpx.Request]:
    """Configure a server the capture's own env client talks to through ``handler``."""

    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://testserver")
    monkeypatch.setenv("LAB_TRACKER_ACCESS_TOKEN", "token-1")

    def from_env(**_kwargs: object) -> LabTracker:
        return LabTracker(
            base_url="http://testserver",
            access_token="token-1",
            default_project_id=os.environ.get("LAB_TRACKER_PROJECT_ID"),
            transport=httpx.MockTransport(recording),
        )

    monkeypatch.setattr(figure_module.LabTracker, "from_env", staticmethod(from_env))
    return seen


def _created(request: httpx.Request) -> httpx.Response:
    return httpx.Response(201, json={"data": {"note_id": "note-1", "metadata": {}}})


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[dict, str]:
    lt_cli.main(["capture", "file", *argv])
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.err


def _figure(tmp_path: Path, name: str = "plot.png", payload: bytes = b"png-bytes") -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    return path


def test_imported_capture_prints_the_figure_result_and_stages_the_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LAB_TRACKER_PROJECT_ID", "project-1")
    seen = _serve(monkeypatch, _created)
    figure = _figure(tmp_path)

    payload, _err = _run(
        capsys,
        str(figure),
        "--metadata",
        "figure_autotracked=true",
        "--metadata",
        "capture_language=R",
    )

    assert set(payload) >= FIGURE_RESULT_KEYS
    assert payload["action"] == "imported"
    assert payload["note_id"] == "note-1"
    assert payload["path"] == str(figure.resolve())
    assert payload["client_capture_id"] == "figure:plot.png"
    assert payload["metadata"]["figure_autotracked"] is True
    assert payload["metadata"]["capture_language"] == "R"
    assert payload["notices"] == []
    assert len(seen) == 1
    body = seen[0].content
    assert _multipart_field(body, "project_id") == "project-1"
    assert _multipart_field(body, "status") == "staged"
    assert json.loads(_multipart_field(body, "metadata"))["evidence_capture_kind"] == "figure"


def test_coalesced_capture_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    figure = _figure(tmp_path)
    content_hash = figure_module._bytes_sha256(figure.read_bytes())

    def existing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "note_id": "note-existing",
                    "metadata": {"evidence_content_hash": content_hash},
                }
            },
        )

    _serve(monkeypatch, existing)
    payload, _err = _run(capsys, str(figure), "--project", "project-1")

    assert payload["action"] == "coalesced"
    assert payload["note_id"] == "note-existing"


def test_unreachable_server_queues_the_capture_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    _serve(monkeypatch, offline)
    payload, err = _run(capsys, str(_figure(tmp_path)), "--project", "project-1")

    assert payload["action"] == "queued"
    assert payload["reason"] == "offline_queued"
    queued = Path(payload["queued_event"])
    assert queued.parent == (tmp_path / "outbox").resolve()
    assert json.loads(queued.read_text())["context"]["project_id"] == "project-1"
    # The notice reaches the terminal and the JSON, so a background caller
    # that discards stderr can still show it once.
    assert any("queued" in notice for notice in payload["notices"])
    assert "queued" in err


def test_unconfigured_capture_is_skipped_with_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload, err = _run(capsys, str(_figure(tmp_path)), "--project", "project-1")

    assert payload["action"] == "skipped"
    assert payload["reason"] == "unconfigured"
    assert "unconfigured" in err
    assert any("unconfigured" in notice for notice in payload["notices"])


def test_capture_of_a_missing_file_fails_softly_with_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload, _err = _run(capsys, str(tmp_path / "never-saved.png"), "--project", "project-1")

    assert payload["action"] == "failed"
    assert payload["reason"] == "capture_failed"
    assert payload["errors"]


def test_server_refusal_is_a_failed_result_not_an_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": {"message": "bad metadata"}})

    _serve(monkeypatch, refuse)
    payload, _err = _run(capsys, str(_figure(tmp_path)), "--project", "project-1")

    assert payload["action"] == "failed"
    assert "bad metadata" in " ".join(payload["errors"])


def test_require_bound_skips_a_save_whose_project_is_only_a_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An .Rprofile hook fires in every directory: with --require-bound a save
    outside a bound checkout sends and queues nothing, even though the
    connection profile names a default project."""

    config_dir = tmp_path / "lt-config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"base_url": "http://testserver", "default_project_id": "project-default"})
    )
    seen = _serve(monkeypatch, _created)
    checkout = _git_repo(tmp_path / "analysis")

    payload, err = _run(capsys, str(_figure(checkout)), "--require-bound")

    assert payload["action"] == "skipped"
    assert payload["reason"] == figure_module.AUTOTRACK_UNBOUND_REASON
    assert seen == []
    assert not (tmp_path / "outbox").exists()
    assert "lt project bind" in err
    assert any("Nothing was sent or queued" in notice for notice in payload["notices"])


def test_require_bound_captures_into_the_checkout_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _serve(monkeypatch, _created)
    checkout = _git_repo(tmp_path / "analysis")
    (checkout / "lt_ids.json").write_text(json.dumps({"project_id": "project-checkout"}))

    payload, _err = _run(capsys, str(_figure(checkout)), "--require-bound")

    assert payload["action"] == "imported"
    assert _multipart_field(seen[0].content, "project_id") == "project-checkout"


@pytest.mark.parametrize("source", ["argument", "environment"])
def test_require_bound_accepts_an_explicit_or_environment_project(
    source: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen = _serve(monkeypatch, _created)
    argv = [str(_figure(tmp_path)), "--require-bound"]
    if source == "argument":
        argv += ["--project", "project-bound"]
    else:
        monkeypatch.setenv("LAB_TRACKER_PROJECT_ID", "project-bound")

    payload, _err = _run(capsys, *argv)

    assert payload["action"] == "imported"
    assert _multipart_field(seen[0].content, "project_id") == "project-bound"


def test_kind_and_logical_id_shape_the_capture_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _serve(monkeypatch, _created)
    table = _figure(tmp_path, "summary.csv", b"a,b\n1,2\n")

    payload, _err = _run(
        capsys,
        str(table),
        "--kind",
        "table",
        "--logical-id",
        "results/summary",
        "--project",
        "project-1",
    )

    assert payload["client_capture_id"] == "table:results/summary"
    metadata = json.loads(_multipart_field(seen[0].content, "metadata"))
    assert metadata["evidence_capture_kind"] == "table"
    assert metadata["table_client_capture_id"] == "table:results/summary"


def test_metadata_values_are_typed_only_when_they_round_trip() -> None:
    parse = cli_capture.parse_metadata_item
    assert parse("figure_autotracked=true") == ("figure_autotracked", True)
    assert parse("flag=false") == ("flag", False)
    assert parse("count=3") == ("count", 3)
    assert parse("ratio=1.5") == ("ratio", 1.5)
    assert parse("capture_language=R") == ("capture_language", "R")
    # Anything that would not print back identically stays a string.
    assert parse("sample=007") == ("sample", "007")
    assert parse("ratio=1.50") == ("ratio", "1.50")
    assert parse("flag=True") == ("flag", "True")
    assert parse("expr=a=b") == ("expr", "a=b")
    assert parse("empty=") == ("empty", "")


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["plot.png", "--metadata", "no-equals-sign"],
        ["plot.png", "--metadata", "=value"],
        ["plot.png", "--kind", ""],
        ["plot.png", "--kind", "Figure Kind"],
        ["plot.png", "--no-such-flag"],
    ],
    ids=["no-path", "no-equals", "empty-key", "empty-kind", "bad-kind", "unknown-flag"],
)
def test_usage_errors_exit_nonzero_without_capturing(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _serve(monkeypatch, _created)
    _figure(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        lt_cli.main(["capture", "file", *argv])
    assert excinfo.value.code not in (0, None)
    assert seen == []


def test_output_file_receives_the_same_result_atomically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve(monkeypatch, _created)
    output = tmp_path / "results" / "capture.json"
    output.parent.mkdir()

    payload, _err = _run(
        capsys, str(_figure(tmp_path)), "--project", "project-1", "--output", str(output)
    )

    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert [path.name for path in output.parent.iterdir()] == ["capture.json"]


def test_unwritable_output_file_does_not_change_the_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve(monkeypatch, _created)
    output = tmp_path / "missing-dir" / "capture.json"

    payload, err = _run(
        capsys, str(_figure(tmp_path)), "--project", "project-1", "--output", str(output)
    )

    assert payload["action"] == "imported"
    assert not output.exists()
    assert "could not write" in err


def test_real_process_exit_codes(tmp_path: Path) -> None:
    """The contract other runtimes rely on, checked on a real `lt` process."""

    env = {key: value for key, value in os.environ.items() if not key.startswith("LAB_TRACKER_")}
    env["LAB_TRACKER_CONFIG_DIR"] = str(tmp_path / "lt-config")
    env["LAB_TRACKER_WATCH_OUTBOX"] = str(tmp_path / "outbox")
    command = [sys.executable, "-m", "lab_tracker_client", "capture", "file"]

    failed = subprocess.run(  # noqa: S603 - fixed interpreter and module.
        [*command, str(tmp_path / "missing.png"), "--require-bound"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )
    assert failed.returncode == 0, failed.stderr
    assert json.loads(failed.stdout)["action"] in {"failed", "skipped"}

    usage = subprocess.run(  # noqa: S603 - fixed interpreter and module.
        [*command, "plot.png", "--metadata", "missing-equals"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )
    assert usage.returncode == 2
    assert usage.stdout == ""
