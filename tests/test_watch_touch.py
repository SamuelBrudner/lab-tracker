"""`lt watch touch`: event-driven watch capture for files an agent writes."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import httpx
import pytest

import lab_tracker_client.agent_session as agent_session
import lab_tracker_client.watch_touch as watch_touch
from lab_tracker_client import LabTracker
from lab_tracker_client import cli as lt_cli
from lab_tracker_client import watch as watch_capture


@pytest.fixture
def touch_env(monkeypatch, tmp_path: Path) -> Path:
    for key in (
        "LAB_TRACKER_PROJECT_ID",
        "LAB_TRACKER_WATCH_CONFIG",
        "LAB_TRACKER_WATCH_OUTBOX",
        "LAB_TRACKER_AGENT_HOOKS",
        "LAB_TRACKER_SESSION_CONTEXT",
        "LAB_TRACKER_BASE_URL",
        "CLAUDE_PROJECT_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    return path.resolve()


def _watched_repo(tmp_path: Path, *, project_id: str | None = "project-touch", **watch) -> Path:
    repo = _git_repo(tmp_path / "analysis")
    (repo / "results").mkdir()
    (repo / "src").mkdir()
    entry = {"root": "results", "mode": "files", "sink": "staged-note", **watch}
    config: dict[str, object] = {
        "version": 1,
        "outbox": ".lab-tracker/outbox/watch",
        "watches": [entry],
    }
    if project_id:
        config["project_id"] = project_id
    (repo / ".lab-tracker").mkdir()
    (repo / ".lab-tracker" / "watch.json").write_text(json.dumps(config), encoding="utf-8")
    return repo


def _post_tool_use(repo: Path, file_path: Path | str, tool_name: str = "Write") -> dict:
    return {
        "session_id": "s-1",
        "cwd": str(repo),
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": {"file_path": str(file_path), "content": "..."},
        "tool_response": {"type": "text", "text": "ok"},
    }


def _events(repo: Path) -> list[Path]:
    return sorted((repo / ".lab-tracker" / "outbox" / "watch").glob("*.json"))


def _no_client():
    raise AssertionError("an unwatched or queue-only touch must not build a client")


def _forbid_scans_and_network(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("fast path must not scan folders, hash files, or sync")

    for name in ("discover_files", "scan_watch", "scan_configured", "event_from_file"):
        monkeypatch.setattr(watch_capture, name, forbidden)
    monkeypatch.setattr(watch_touch, "drain_watch_outbox", forbidden)
    monkeypatch.setattr(LabTracker, "from_env", classmethod(lambda _cls, **_kw: forbidden()))


def test_unwatched_path_returns_without_scanning_or_network(touch_env: Path, monkeypatch) -> None:
    repo = _watched_repo(touch_env, include=["*.csv"])
    source = repo / "src" / "fit.py"
    source.write_text("x = 1\n", encoding="utf-8")
    _forbid_scans_and_network(monkeypatch)

    payload = watch_touch.touch_from_hook(_post_tool_use(repo, source))

    assert payload["action"] == "unwatched"
    assert _events(repo) == []


def test_missing_or_parent_directory_config_is_a_no_op(touch_env: Path, monkeypatch) -> None:
    # A lab-wide config above the checkout never claims an agent's writes.
    (touch_env / ".lab-tracker").mkdir()
    (touch_env / ".lab-tracker" / "watch.json").write_text(
        json.dumps({"version": 1, "watches": [{"root": str(touch_env), "mode": "files"}]}),
        encoding="utf-8",
    )
    repo = _git_repo(touch_env / "plain")
    target = repo / "notes.md"
    target.write_text("hi", encoding="utf-8")
    _forbid_scans_and_network(monkeypatch)

    payload = watch_touch.touch_from_hook(_post_tool_use(repo, target))

    assert payload["action"] == "no-config"


def test_read_tool_and_missing_paths_are_never_captured(touch_env: Path, monkeypatch) -> None:
    repo = _watched_repo(touch_env)
    target = repo / "results" / "a.csv"
    target.write_text("1,2\n", encoding="utf-8")
    _forbid_scans_and_network(monkeypatch)

    read = watch_touch.touch_from_hook(_post_tool_use(repo, target, tool_name="Read"))
    empty = watch_touch.touch_from_hook({"hook_event_name": "PostToolUse", "cwd": str(repo)})

    assert read["action"] == "no-path"
    assert empty["action"] == "no-path"
    assert _events(repo) == []


def test_hidden_and_excluded_files_stay_unwatched(touch_env: Path, monkeypatch) -> None:
    repo = _watched_repo(touch_env, include=["*.csv"], exclude=["scratch/*"])
    hidden = repo / "results" / ".partial.csv"
    excluded = repo / "results" / "scratch" / "tmp.csv"
    wrong_type = repo / "results" / "notes.txt"
    excluded.parent.mkdir()
    for path in (hidden, excluded, wrong_type):
        path.write_text("x", encoding="utf-8")
    _forbid_scans_and_network(monkeypatch)

    for path in (hidden, excluded, wrong_type):
        assert watch_touch.touch_from_hook(_post_tool_use(repo, path))["action"] == "unwatched"


def test_watched_file_queues_the_same_event_a_scan_would(touch_env: Path) -> None:
    repo = _watched_repo(touch_env, include=["*.csv"], tags=["agent"])
    target = repo / "results" / "dose.csv"
    target.write_text("dose,response\n1,2\n", encoding="utf-8")

    payload = watch_touch.touch_from_hook(
        _post_tool_use(repo, "results/dose.csv"), sync=False, client_factory=_no_client
    )

    assert payload["action"] == "queued"
    events = _events(repo)
    assert [Path(payload["results"][0]["event_path"])] == events
    event = watch_capture.read_event(events[0])
    assert event["context"]["project_id"] == "project-touch"
    assert event["context"]["tags"] == ["agent"]
    assert event["source"]["relative_path"] == "dose.csv"

    # The scheduled scan sees the same event and queues nothing new.
    config = watch_capture.load_config(config_path=repo / ".lab-tracker" / "watch.json")
    scan = watch_capture.scan_watch(
        config, mode="files", root=repo / "results", include_patterns=["*.csv"], tags=["agent"]
    )
    assert scan["imported"][0]["already_present"] is True
    assert _events(repo) == events

    again = watch_touch.touch_from_hook(
        _post_tool_use(repo, target), sync=False, client_factory=_no_client
    )
    assert again["action"] == "already-queued"


def test_manifest_watch_queues_a_written_manifest(touch_env: Path) -> None:
    repo = _watched_repo(touch_env, mode="manifest")
    run = repo / "results" / "run-1"
    run.mkdir()
    manifest = run / "lab-tracker-evidence.json"
    manifest.write_text(json.dumps({"capture_id": "run-1", "title": "Run 1"}), encoding="utf-8")
    other = run / "log.txt"
    other.write_text("log", encoding="utf-8")

    assert watch_touch.touch_from_hook(_post_tool_use(repo, other))["action"] == "unwatched"
    payload = watch_touch.touch_from_hook(_post_tool_use(repo, manifest), sync=False)

    assert payload["action"] == "queued"
    event = watch_capture.read_event(_events(repo)[0])
    assert event["capture_id"] == "run-1"


def test_staged_note_capture_needs_a_declared_project(touch_env: Path, capsys) -> None:
    repo = _watched_repo(touch_env, project_id=None)
    target = repo / "results" / "a.csv"
    target.write_text("1", encoding="utf-8")

    unbound = watch_touch.touch_from_hook(_post_tool_use(repo, target), sync=False)

    assert unbound["action"] == "unbound"
    assert _events(repo) == []
    assert "lt project bind" in capsys.readouterr().err

    (repo / "lt_ids.json").write_text(json.dumps({"project_id": "project-ids"}), encoding="utf-8")
    bound = watch_touch.touch_from_hook(_post_tool_use(repo, target), sync=False)
    assert bound["action"] == "queued"


def test_touch_syncs_best_effort_and_keeps_events_when_offline(touch_env: Path) -> None:
    repo = _watched_repo(touch_env)
    first = repo / "results" / "a.csv"
    first.write_text("1", encoding="utf-8")
    requests: list[str] = []

    def online(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/notes":
            return httpx.Response(
                200, json={"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        if request.url.path == "/notes/upload-file":
            return httpx.Response(201, json={"data": {"note_id": "note-a", "metadata": {}}})
        return httpx.Response(500, json={"error": {"message": "unexpected"}})

    synced = watch_touch.touch_from_hook(
        _post_tool_use(repo, first),
        client_factory=lambda: LabTracker(
            base_url="http://testserver", transport=httpx.MockTransport(online)
        ),
    )
    assert synced["sync"]["errors"] == []
    assert requests == ["/health", "/notes", "/notes/upload-file"]
    assert watch_capture.read_event(_events(repo)[0])["sync"]["status"] == "synced"

    second = repo / "results" / "b.csv"
    second.write_text("2", encoding="utf-8")

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    queued = watch_touch.touch_from_hook(
        _post_tool_use(repo, second),
        client_factory=lambda: LabTracker(
            base_url="http://testserver", transport=httpx.MockTransport(offline)
        ),
    )
    assert queued["action"] == "queued"
    assert "sync_error" in queued
    pending = [watch_capture.read_event(path)["sync"]["status"] for path in _events(repo)]
    assert sorted(pending) == ["pending", "synced"]


def test_kill_switch_turns_touch_off(touch_env: Path, monkeypatch) -> None:
    repo = _watched_repo(touch_env)
    target = repo / "results" / "a.csv"
    target.write_text("1", encoding="utf-8")
    monkeypatch.setenv(agent_session.AGENT_HOOKS_ENV, "off")

    assert watch_touch.touch_from_hook(_post_tool_use(repo, target))["action"] == "disabled"
    assert _events(repo) == []


def test_lt_watch_touch_cli_hook_stdin_is_quiet_and_paths_print_json(
    touch_env: Path, monkeypatch, capsys
) -> None:
    repo = _watched_repo(touch_env)
    target = repo / "results" / "a.csv"
    target.write_text("1", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_post_tool_use(repo, target))))

    lt_cli.main(["watch", "touch", "--fail-silent", "--no-sync"])

    assert capsys.readouterr().out == ""
    assert len(_events(repo)) == 1

    lt_cli.main(["watch", "touch", str(target), "--repo", str(repo), "--no-sync"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "watch-touch"
    assert payload["action"] == "already-queued"
