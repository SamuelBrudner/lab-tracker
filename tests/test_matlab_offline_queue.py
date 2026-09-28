"""The MATLAB client's offline queue feeds the same `lt outbox sync` as Python.

``tests/fixtures/matlab/offline_figure_event.json`` is written by hand to be
exactly what ``matlab/+labtracker/+internal/queueOffline.m`` writes (compact
``jsonencode`` output, checked against a GNU Octave run of that code): a figure
saved while the server was down, in a checkout bound to a project, with a
checkout session active and git run facts recorded. The figure bytes are
``b"PNGDATA"`` so its content hash and size hold for any copy of the file.

Where ``octave-cli`` is installed the last test runs the MATLAB code itself.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

import lab_tracker_client.figure as figure_module
from lab_tracker_client import LabTracker, watch
from lab_tracker_client import cli as lt_cli
from lab_tracker_client.figure import _reset_figure_capture_state_for_tests
from lab_tracker_client.session_context import encode_session_link_code

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "matlab" / "offline_figure_event.json"
MATLAB_ROOT = ROOT / "matlab"
FIGURE_BYTES = b"PNGDATA"
PROJECT_ID = "4a0c2f55-8f5e-4c3b-9d7a-1f2e3d4c5b6a"
SESSION_ID = "3d4f6a1e-9c2b-4a8e-8f01-2b3c4d5e6f70"
OCTAVE = shutil.which("octave-cli")


def _multipart_field(body: bytes, name: str) -> str:
    marker = f'name="{name}"'.encode()
    assert marker in body
    chunk = body.split(marker, 1)[1].split(b"\r\n\r\n", 1)[1]
    return chunk.split(b"\r\n--", 1)[0].decode("utf-8")


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _reset_figure_capture_state_for_tests()
    for key in [key for key in os.environ if key.startswith("LAB_TRACKER_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-config"))
    yield
    _reset_figure_capture_state_for_tests()


def _checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "analysis"
    (repo / "figs").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)  # noqa: S603, S607
    (repo / "lt_ids.json").write_text(json.dumps({"project_id": PROJECT_ID}), encoding="utf-8")
    (repo / "figs" / "trace.png").write_bytes(FIGURE_BYTES)
    return repo


def _matlab_event_in(repo: Path) -> Path:
    """Place the fixture in ``repo``'s outbox as MATLAB would have written it there."""

    event = json.loads(FIXTURE.read_text(encoding="utf-8"))
    figure = (repo / "figs" / "trace.png").resolve()
    root = repo.resolve()
    event["source"].update(
        {"path": str(figure), "uri": figure.as_uri(), "root": str(root), "root_uri": root.as_uri()}
    )
    outbox = root / ".lab-tracker" / "outbox" / "watch"
    outbox.mkdir(parents=True)
    # queueOffline.m names the file as watch.event_path does; the Octave test
    # checks that on MATLAB's own output.
    path = outbox / "figure-figs-trace.png.staged-note.figure-2d4566582844690f.json"
    path.write_text(json.dumps(event, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def _python_queued_event(tmp_path: Path) -> dict:
    """What the Python client queues for the same save, for a schema comparison."""

    repo = tmp_path / "python-checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)  # noqa: S603, S607
    figure = repo / "trace.png"
    figure.write_bytes(FIGURE_BYTES)
    # The session `lt session use` records for this checkout and project.
    session = {
        "session_id": SESSION_ID,
        "link_code": encode_session_link_code(SESSION_ID),
        "project_id": PROJECT_ID,
        "source": "checkout",
    }
    queued = figure_module._queue_capture_offline(
        path=figure,
        kind="figure",
        project_id=PROJECT_ID,
        session=session,
        client_capture_id="figure:trace.png",
        content_hash="0" * 64,
        size_bytes=len(FIGURE_BYTES),
        metadata={"figure_full_size_bytes": len(FIGURE_BYTES)},
        reason="offline",
    )
    assert queued is not None
    return json.loads(queued.read_text(encoding="utf-8"))


def test_matlab_event_matches_the_python_queued_event_schema(tmp_path: Path) -> None:
    matlab = json.loads(FIXTURE.read_text(encoding="utf-8"))
    python = _python_queued_event(tmp_path)

    validated = watch.validate_event(matlab)
    assert validated["context"] == {
        "project_id": PROJECT_ID,
        "question_id": None,
        "dataset_ids": [],
        "tags": [],
        "session_id": SESSION_ID,
    }
    assert set(matlab) == set(python)
    assert set(matlab["payload"]) == set(python["payload"])
    assert set(matlab["context"]) <= set(python["context"])
    # Everything but mtime: MATLAB cannot reproduce Python's float st_mtime.
    assert set(matlab["source"]) == set(python["source"]) - {"mtime"}
    assert (matlab["capture_kind"], matlab["sink"]) == (python["capture_kind"], python["sink"])
    assert matlab["event_id"] == f"figure-{matlab['source']['content_hash'][:16]}"
    assert matlab["payload"]["client_capture_id"] == matlab["capture_id"]
    assert not any(key.startswith("evidence_") for key in matlab["payload"]["metadata"])
    assert "declared_target_source" not in matlab["payload"]["metadata"]
    assert matlab["payload"]["metadata"]["capture_session_link_code"] == encode_session_link_code(
        SESSION_ID
    )


def test_lt_outbox_sync_delivers_a_matlab_queued_figure_like_a_python_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _checkout(tmp_path)
    event_path = _matlab_event_in(repo)
    assert watch.event_path(watch.read_event(event_path), event_path.parent) == event_path

    uploads: list[bytes] = []

    def server(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/notes":
            return httpx.Response(
                200, json={"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            uploads.append(request.content)
            return httpx.Response(201, json={"data": {"note_id": "note-matlab"}})
        return httpx.Response(500, json={"error": {"message": "unexpected"}})

    class _FromEnv:
        @staticmethod
        def from_env(**_kwargs: object) -> LabTracker:
            return LabTracker(base_url="http://testserver", transport=httpx.MockTransport(server))

    monkeypatch.setattr(lt_cli, "LabTracker", _FromEnv)
    lt_cli.main(["outbox", "sync", "--repo", str(repo)])
    capsys.readouterr()

    assert len(uploads) == 1
    body = uploads[0]
    assert FIGURE_BYTES in body
    assert _multipart_field(body, "project_id") == PROJECT_ID
    assert _multipart_field(body, "status") == "staged"
    assert _multipart_field(body, "client_capture_id") == "figure:figs/trace.png"
    # The checkout session was recorded for this project, so it is declared,
    # with the weaker label a bounded default gets.
    assert json.loads(_multipart_field(body, "targets")) == [
        {"entity_type": "session", "entity_id": SESSION_ID}
    ]
    metadata = json.loads(_multipart_field(body, "metadata"))
    assert metadata["evidence_adapter"] == "lab-tracker-matlab-figure"
    assert metadata["evidence_source_provider"] == "local-figure"
    assert metadata["evidence_source_external_id"] == "figure:figs/trace.png"
    assert metadata["evidence_content_hash"] == (
        "2d4566582844690f8634a8b2534ea5221560038c6c0650c99140759bad603ae2"
    )
    assert metadata["declared_target_source"] == "config_default"
    assert metadata["watch_session_source"] == "active"
    assert metadata["capture_session_id"] == SESSION_ID
    assert metadata["run_git_commit"] == "cb62d8dc3b2525f64a11880413c1a25e54c386c5"
    assert metadata["run_git_dirty"] is True
    assert metadata["run_repo_remote_url"] == "github.com/lab/analysis"
    assert metadata["capture_host_label"] == "bench-pc"
    assert metadata["figure_full_size_bytes"] == len(FIGURE_BYTES)
    sync = watch.read_event(event_path)["sync"]
    assert (sync["status"], sync["note_id"]) == ("synced", "note-matlab")


def test_a_session_recorded_for_another_project_stays_plain_metadata(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    event_path = _matlab_event_in(repo)
    event = watch.read_event(event_path)
    event["source"]["session_project_id"] = "another-project"

    assert watch._declared_targets(event, project_id=PROJECT_ID) == []
    assert watch._event_metadata(event, project_id=PROJECT_ID)["watch_session_id"] == SESSION_ID


def test_a_matlab_event_goes_stale_when_the_figure_changes_before_sync(tmp_path: Path) -> None:
    """Without an mtime the content hash and size still decide staleness."""

    repo = _checkout(tmp_path)
    event_path = _matlab_event_in(repo)
    assert watch._stale_reason(watch.read_event(event_path)) == ""

    (repo / "figs" / "trace.png").write_bytes(b"PNGDATB")
    assert "changed since scan" in watch._stale_reason(watch.read_event(event_path))
    (repo / "figs" / "trace.png").unlink()
    assert "missing" in watch._stale_reason(watch.read_event(event_path))


OCTAVE_SCRIPT = """
addpath(getenv('LT_MATLAB_ROOT'));
fake = struct('ProjectId', '', 'BaseUrl', 'http://127.0.0.1:9', ...
    'uploadFigure', @(varargin) error('MATLAB:webservices:ConnectionRefused', ...
        'Could not connect to http://127.0.0.1:9: connection refused'));
warning('off', 'all');
first = labtracker.uploadFigure(getenv('LT_FIGURE'), 'Client', fake, ...
    'Metadata', struct('analysis_name', 'octave'));
second = labtracker.uploadFigure(getenv('LT_FIGURE'), 'Client', fake);
printf('%s\\n%s\\n%s\\n%s\\n', first.action, first.queued_event, second.action, second.reason);
"""


@pytest.mark.skipif(OCTAVE is None, reason="GNU Octave (octave-cli) is not installed")
def test_octave_runs_the_matlab_offline_queue_end_to_end(tmp_path: Path) -> None:
    """The real +labtracker code, under Octave with a client whose upload
    fails the way MATLAB's HTTP layer does when the server is down: the save
    is queued into the checkout's outbox, a second save is queued without
    another connection attempt, and Python reads and names the event the
    same way."""

    repo = _checkout(tmp_path)
    subprocess.run(  # noqa: S603, S607 - fixed git command in a temp repo.
        ["git", "-C", str(repo), "remote", "add", "origin", "https://tok@github.com/Lab/Analysis"],
        check=True,
    )
    script = tmp_path / "queue.m"
    script.write_text(OCTAVE_SCRIPT, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("LAB_TRACKER_")}
    env.update(
        {
            "LT_MATLAB_ROOT": str(MATLAB_ROOT),
            "LT_FIGURE": str(repo / "figs" / "trace.png"),
            "LAB_TRACKER_SESSION_ID": encode_session_link_code(SESSION_ID),
        }
    )
    assert OCTAVE is not None
    completed = subprocess.run(  # noqa: S603 - fixed octave-cli, test-authored script.
        [OCTAVE, "--no-gui", "--quiet", str(script)],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    action, queued, second_action, second_reason = completed.stdout.strip().splitlines()[-4:]
    assert (action, second_action, second_reason) == ("queued", "queued", "offline_queued")

    event_path = Path(queued)
    outbox = repo.resolve() / ".lab-tracker" / "outbox" / "watch"
    assert event_path.parent == outbox
    assert [path.name for path in outbox.iterdir()] == [event_path.name]
    event = watch.read_event(event_path)
    assert watch.event_path(event, outbox) == event_path
    assert watch._stale_reason(event) == ""

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw = json.loads(event_path.read_text(encoding="utf-8"))
    assert set(raw) == set(fixture)
    assert set(raw["source"]) == set(fixture["source"]) - {"session_project_id"}
    assert set(raw["payload"]) == set(fixture["payload"])
    assert event["context"]["project_id"] == PROJECT_ID
    assert event["context"]["session_id"] == SESSION_ID
    assert event["source"]["session_context"] == "env"
    assert event["source"]["content_hash"] == fixture["source"]["content_hash"]
    metadata = event["payload"]["metadata"]
    assert metadata["analysis_name"] == "octave"
    assert metadata["run_repo_remote_url"] == "github.com/lab/analysis"
    assert metadata["run_git_dirty"] is True
    assert "run_git_commit" not in metadata  # a repository without a commit yet
    assert metadata["capture_session_link_code"] == encode_session_link_code(SESSION_ID)
    assert set(fixture["payload"]["metadata"]) - {"analysis_name"} <= set(metadata) | {
        "run_git_commit"
    }


CONTEXT_SCRIPT = """
addpath(getenv('LT_MATLAB_ROOT'));
[project, source] = labtracker.internal.captureProject(getenv('LT_FIGURE'), '');
session = labtracker.internal.activeSession();
if isempty(session)
    printf('%s\\n%s\\n-\\n-\\n', project, source);
else
    printf('%s\\n%s\\n%s\\n%s\\n', project, source, session.session_id, session.source);
end
"""


def _octave_context(repo: Path, env_extra: dict[str, str]) -> list[str]:
    script = repo.parent / "context.m"
    script.write_text(CONTEXT_SCRIPT, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("LAB_TRACKER_")}
    env.update({"LT_MATLAB_ROOT": str(MATLAB_ROOT), "LT_FIGURE": str(repo / "figs" / "trace.png")})
    env.update(env_extra)
    assert OCTAVE is not None
    completed = subprocess.run(  # noqa: S603 - fixed octave-cli, test-authored script.
        [OCTAVE, "--no-gui", "--quiet", str(script)],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed.stdout.strip().splitlines()[-4:]


@pytest.mark.skipif(OCTAVE is None, reason="GNU Octave (octave-cli) is not installed")
def test_octave_resolves_project_and_session_like_the_python_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lab_tracker_client.capture_project import resolve_capture_project
    from lab_tracker_client.session_context import read_active_session

    repo = _checkout(tmp_path)
    session_file = repo / ".lab-tracker" / "session.json"
    session_file.parent.mkdir()

    def both(env_extra: dict[str, str]) -> tuple[list[str], list[str]]:
        for key, value in env_extra.items():
            monkeypatch.setenv(key, value)
        monkeypatch.chdir(repo)
        project = resolve_capture_project(repo / "figs" / "trace.png", project_id=None)
        active = read_active_session()
        python = [
            project.project_id if project else "",
            project.source.value if project else "",
            active["session_id"] if active else "-",
            active["source"] if active else "-",
        ]
        for key in env_extra:
            monkeypatch.delenv(key)
        return _octave_context(repo, env_extra), python

    # A live checkout session recorded by `lt session use`, named by link code.
    session_file.write_text(
        json.dumps(
            {
                "version": 1,
                "session_id": encode_session_link_code(SESSION_ID),
                "project_id": PROJECT_ID,
                "expires_at": "2999-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    octave, python = both({})
    assert octave == python == [PROJECT_ID, "checkout", SESSION_ID, "checkout"]

    # The environment wins for both the project and the session.
    other_session = "0f0e0d0c-0b0a-4908-8706-050403020100"
    octave, python = both(
        {"LAB_TRACKER_PROJECT_ID": "project-env", "LAB_TRACKER_SESSION_ID": other_session}
    )
    assert octave == python == ["project-env", "environment", other_session, "env"]

    # An expired session is no session; a checkout named only by its watch
    # config still resolves to that project, as an unbound source.
    session_file.write_text(
        json.dumps({"session_id": SESSION_ID, "expires_at": "2001-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    (repo / "lt_ids.json").unlink()
    (repo / ".lab-tracker" / "watch.json").write_text(
        json.dumps({"version": 1, "project_id": "project-watch", "watches": []}),
        encoding="utf-8",
    )
    octave, python = both({})
    assert octave == python == ["project-watch", "watch_config", "-", "-"]
