"""Which capture paths record the capturing client's release and install id (GH #238).

Only a capture that carries them can name a stale client in the Daily review, so
docs/setup.md lists the paths that record them. The stamp is written by
``capture_host_metadata``; this pins who reaches it by behaviour (the event
builders, and the commands whose notes are drained from them), by construction
(the client modules that reach it), and that the docs name the same set.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

import lab_tracker_client.figure as figure_module
from lab_tracker_client import LabTracker, hpc, pipeline_capture, repo, watch
from lab_tracker_client.figure import capture_figure_bytes
from lab_tracker_client.pipeline_capture import PipelineRun, report_pipeline_run
from lab_tracker_client.run_capture import RunOptions, run_command
from lab_tracker_client.watch import sync_outbox_path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CLIENT_DIR = _REPO_ROOT / "src" / "lab_tracker_client"
_SETUP_DOC = _REPO_ROOT / "docs" / "setup.md"

PROJECT_ID = "project-stamp"
# What the coverage read needs from a note to judge its client (capture_client_release).
STAMP_KEYS = ("capture_install_id", "capture_client_version")

# Each client module that records the release, with the command docs/setup.md
# names for it. A module records it by calling ``capture_host_metadata()`` itself
# or by building its event with ``make_event``; the docs name the command, so
# this is the list they must keep in step with.
_STAMPING_MODULES = {
    "agent_session.py": "lt agent session-end",
    "figure.py": "lt capture",
    "git_capture.py": "lt git snapshot",
    "hpc.py": "lt hpc",
    "notebook_capture.py": "Jupyter save hook",
    "pipeline_capture.py": "lt pipeline report",
    "repo.py": "lt repo report",
    "run_capture.py": "lt run",
    "watch.py": "lt watch",
}
# Written by hand or imported, so they carry no install id and no client release.
_UNSTAMPED_PATHS = (
    "lt note",
    "lt quick",
    "lt import-folder",
    "upsert_note",
    "quick_capture",
    "upload_note_file",
    "MATLAB",
)
_STAMPING_CALL = re.compile(r"(?<!def )\b(?:capture_host_metadata|make_event)\(")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    figure_module._reset_figure_capture_state_for_tests()
    pipeline_capture._reset_notices_for_tests()
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-config"))
    for name in (
        "LAB_TRACKER_BASE_URL",
        "LAB_TRACKER_MCP_BASE_URL",
        "LAB_TRACKER_ACCESS_TOKEN",
        "LAB_TRACKER_SESSION_ID",
        "LAB_TRACKER_SESSION_CONTEXT",
        "LAB_TRACKER_WATCH_CONFIG",
        "LAB_TRACKER_WATCH_OUTBOX",
        "LAB_TRACKER_CAPTURE_OUTBOX",
        "LAB_TRACKER_PIPELINE_CAPTURE",
        "LAB_TRACKER_HPC_CONFIG",
        "LAB_TRACKER_HPC_OUTBOX",
        "LAB_TRACKER_HPC_RUN_ID",
        "LAB_TRACKER_REPO_CONFIG",
        "LAB_TRACKER_REPO_OUTBOX",
        "LAB_TRACKER_REPO_RUN_ID",
        "LAB_TRACKER_CONTAINER_REF",
        "LAB_TRACKER_WORKTREE_TREE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LAB_TRACKER_PROJECT_ID", PROJECT_ID)
    monkeypatch.chdir(tmp_path)
    yield
    figure_module._reset_figure_capture_state_for_tests()
    pipeline_capture._reset_notices_for_tests()


def _multipart_field(body: bytes, name: str) -> str:
    marker = f'name="{name}"'.encode()
    assert marker in body
    chunk = body.split(marker, 1)[1].split(b"\r\n\r\n", 1)[1]
    return chunk.split(b"\r\n--", 1)[0].decode("utf-8")


class _Server:
    """A Lab Tracker that accepts uploads and keeps each note's metadata."""

    def __init__(self) -> None:
        self.metadata: list[dict[str, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "GET" and request.url.path == "/notes":
            return httpx.Response(200, json={"data": [], "meta": {"total": 0}})
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            self.metadata.append(json.loads(_multipart_field(request.content, "metadata")))
            return httpx.Response(201, json={"data": {"note_id": "note-1", "project_id": "p"}})
        return httpx.Response(500, json={"error": {"message": "unexpected request"}})

    def client(self) -> LabTracker:
        return LabTracker(base_url="http://testserver", transport=httpx.MockTransport(self.handler))

    def drain(self, outbox: Path) -> dict[str, object]:
        with self.client() as lt:
            summary = sync_outbox_path(lt, outbox)
        assert summary["errors"] == []
        (metadata,) = self.metadata
        return metadata


def _assert_stamped(carrier: object, *, where: str) -> None:
    assert isinstance(carrier, dict), where
    for key in STAMP_KEYS:
        assert carrier.get(key), f"{where} carries no {key}"


def test_the_watch_event_builder_records_the_client_release_and_install_id() -> None:
    event = watch.make_event(
        capture_id="c-1",
        capture_kind="file",
        adapter="lt-watch",
        sink=watch.SINK_STAGED_NOTE,
    )

    _assert_stamped(event["host"], where="watch.make_event")


def test_the_hpc_event_builder_records_the_client_release_and_install_id() -> None:
    config = hpc.init_config(project_id=PROJECT_ID, cluster="generic")

    event = hpc.make_event(config, event_type="begin")

    _assert_stamped(event["host"], where="hpc.make_event")


def test_the_repo_event_builder_records_the_client_release_and_install_id() -> None:
    config = repo.init_config(project_id=PROJECT_ID)

    event = repo.make_event(config, event_type="commit")

    _assert_stamped(event["host"], where="repo.make_event")


def test_an_lt_run_note_carries_the_client_release_and_install_id(tmp_path: Path) -> None:
    code = run_command([sys.executable, "-c", "pass"], RunOptions(drain=False))

    assert code == 0
    metadata = _Server().drain(tmp_path / ".lab-tracker" / "outbox" / "watch")
    assert metadata["watch_adapter"] == "lt-run"
    _assert_stamped(metadata, where="the lt run note")


def test_an_lt_pipeline_note_carries_the_client_release_and_install_id(tmp_path: Path) -> None:
    report_pipeline_run(
        PipelineRun(engine="snakemake", status="success", run_id="run-1"),
        cwd=tmp_path,
        project_id=PROJECT_ID,
        drain=False,
    )

    metadata = _Server().drain(tmp_path / ".lab-tracker" / "outbox" / "watch")
    assert metadata["watch_adapter"] == "lt-pipeline"
    _assert_stamped(metadata, where="the lt pipeline note")


def test_a_figure_capture_note_carries_the_client_release_and_install_id(tmp_path: Path) -> None:
    checkout = tmp_path / "analysis"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)  # noqa: S603, S607
    (checkout / "lt_ids.json").write_text(json.dumps({"project_id": PROJECT_ID}))
    server = _Server()

    with server.client() as lt:
        result = capture_figure_bytes(
            b"\x89PNG\r\n\x1a\nfigure",
            filename="fig.png",
            anchor=checkout,
            source_uri=(checkout / "nb.ipynb").as_uri() + "#display=cell-1/figure-1",
            logical_id="display/nb.ipynb/cell-1/figure-1",
            client=lt,
            require_bound_project=True,
        )

    assert result.action == "imported"
    (metadata,) = server.metadata
    _assert_stamped(metadata, where="the figure capture note")


def _modules_reaching_the_stamp() -> set[str]:
    return {
        path.name
        for path in _CLIENT_DIR.glob("*.py")
        if path.name != "client.py" and _STAMPING_CALL.search(path.read_text(encoding="utf-8"))
    }


def test_the_client_modules_that_record_the_release_are_the_documented_capture_paths() -> None:
    assert _modules_reaching_the_stamp() == set(_STAMPING_MODULES), (
        "a capture path started or stopped recording the client release; update "
        "_STAMPING_MODULES here, docs/setup.md, docs/retained-v1-surface.md, the "
        "Daily review sentence in setup_guide.py, and capture_client_release.py"
    )


def test_setup_docs_name_the_paths_that_record_the_release_and_those_that_do_not() -> None:
    text = " ".join(_SETUP_DOC.read_text(encoding="utf-8").split())
    opening = "- A capture queued through the watch outbox"
    assert opening in text, "docs/setup.md must list the captures that record the release"
    recorded, _, unrecorded = text.split(opening, 1)[1].split("The coverage read", 1)[0].partition(
        "A note made by hand or import"
    )

    assert unrecorded, "docs/setup.md must say which notes carry no client release"
    assert [name for name in _STAMPING_MODULES.values() if name not in recorded] == []
    assert [name for name in _UNSTAMPED_PATHS if name not in unrecorded] == []
