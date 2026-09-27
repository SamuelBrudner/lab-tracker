"""Which project a client-side capture is filed under.

A figure saved inside a checkout bound to project B must never land in the
profile's default project A: the checkout's own binding outranks every
default, exactly as it does for git snapshots.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

import lab_tracker_client.figure as figure_module
from lab_tracker_client import LabTracker, savefig
from lab_tracker_client.capture_project import (
    CaptureProject,
    CaptureProjectSource,
    resolve_capture_project,
)
from lab_tracker_client.figure import _reset_figure_capture_state_for_tests
from lab_tracker_client.watch import read_event

PROJECT_EXPLICIT = "11111111-1111-4111-8111-111111111111"
PROJECT_ENV = "22222222-2222-4222-8222-222222222222"
PROJECT_CHECKOUT = "33333333-3333-4333-8333-333333333333"
PROJECT_WATCH = "44444444-4444-4444-8444-444444444444"
PROJECT_DEFAULT = "55555555-5555-4555-8555-555555555555"
UNREACHABLE_BASE_URL = "http://127.0.0.1:9"


class FakeFigure:
    def __init__(self, payload: bytes = b"figure-bytes") -> None:
        self.payload = payload

    def savefig(self, path: str | Path, **_kwargs: object) -> None:
        Path(path).write_bytes(self.payload)


def _git_init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    return path.resolve()


def _bind_checkout(repo: Path, project_id: str) -> None:
    (repo / "lt_ids.json").write_text(json.dumps({"project_id": project_id}), encoding="utf-8")


def _bind_watch_config(repo: Path, project_id: str) -> None:
    config_dir = repo / ".lab-tracker"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "watch.json").write_text(
        json.dumps({"version": 1, "project_id": project_id, "watches": []}), encoding="utf-8"
    )


def _multipart_field(body: bytes, name: str) -> str:
    marker = f'name="{name}"'.encode()
    chunk = body.split(marker, 1)[1].split(b"\r\n\r\n", 1)[1]
    return chunk.split(b"\r\n--", 1)[0].decode("utf-8")


@pytest.fixture(autouse=True)
def clean_capture_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _reset_figure_capture_state_for_tests()
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-config"))
    for key in (
        "LAB_TRACKER_PROJECT_ID",
        "LAB_TRACKER_BASE_URL",
        "LAB_TRACKER_MCP_BASE_URL",
        "LAB_TRACKER_ACCESS_TOKEN",
        "LAB_TRACKER_USERNAME",
        "LAB_TRACKER_PASSWORD",
        "LAB_TRACKER_SESSION_ID",
        "LAB_TRACKER_SESSION_CONTEXT",
        "LAB_TRACKER_WATCH_CONFIG",
        "LAB_TRACKER_WATCH_OUTBOX",
        "LAB_TRACKER_CAPTURE_OUTBOX",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    _reset_figure_capture_state_for_tests()


def _write_profile(tmp_path: Path, *, default_project_id: str) -> None:
    config_dir = tmp_path / "lt-config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "base_url": UNREACHABLE_BASE_URL,
                "default_project_id": default_project_id,
                "access_token": "profile-token",
            }
        ),
        encoding="utf-8",
    )


def test_explicit_argument_outranks_every_other_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_init(tmp_path / "repo")
    _bind_checkout(repo, PROJECT_CHECKOUT)
    _bind_watch_config(repo, PROJECT_WATCH)
    monkeypatch.setenv("LAB_TRACKER_PROJECT_ID", PROJECT_ENV)

    resolved = resolve_capture_project(repo / "plot.png", project_id=PROJECT_EXPLICIT)

    assert resolved == CaptureProject(
        project_id=PROJECT_EXPLICIT, source=CaptureProjectSource.EXPLICIT
    )
    assert resolved.bound is True


def test_environment_outranks_the_checkout_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_init(tmp_path / "repo")
    _bind_checkout(repo, PROJECT_CHECKOUT)
    monkeypatch.setenv("LAB_TRACKER_PROJECT_ID", PROJECT_ENV)

    resolved = resolve_capture_project(repo / "plot.png", project_id=None)

    assert resolved == CaptureProject(
        project_id=PROJECT_ENV, source=CaptureProjectSource.ENVIRONMENT
    )
    assert resolved.bound is True


def test_checkout_binding_outranks_the_watch_config(tmp_path: Path) -> None:
    repo = _git_init(tmp_path / "repo")
    _bind_checkout(repo, PROJECT_CHECKOUT)
    _bind_watch_config(repo, PROJECT_WATCH)
    (repo / "figs").mkdir()

    resolved = resolve_capture_project(repo / "figs" / "plot.png", project_id=None)

    assert resolved == CaptureProject(
        project_id=PROJECT_CHECKOUT, source=CaptureProjectSource.CHECKOUT
    )
    assert resolved.bound is True


def test_watch_config_project_is_used_when_the_checkout_is_unbound(tmp_path: Path) -> None:
    repo = _git_init(tmp_path / "repo")
    _bind_watch_config(repo, PROJECT_WATCH)

    resolved = resolve_capture_project(repo / "plot.png", project_id=None)

    assert resolved == CaptureProject(
        project_id=PROJECT_WATCH, source=CaptureProjectSource.WATCH_CONFIG
    )
    # A watch config is not the checkout's own binding: autotrack skips it.
    assert resolved.bound is False


def test_nothing_resolves_outside_a_bound_checkout(tmp_path: Path) -> None:
    loose = tmp_path / "loose"
    loose.mkdir()
    assert resolve_capture_project(loose / "plot.png", project_id=None) is None
    repo = _git_init(tmp_path / "repo")
    assert resolve_capture_project(repo / "plot.png", project_id=None) is None


def test_live_capture_in_a_checkout_bound_to_b_uploads_to_b_not_the_default_a(
    tmp_path: Path,
) -> None:
    repo = _git_init(tmp_path / "repo")
    _bind_checkout(repo, PROJECT_CHECKOUT)
    uploaded_to: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        uploaded_to.append(_multipart_field(request.content, "project_id"))
        return httpx.Response(201, json={"data": {"note_id": "note-1", "metadata": {}}})

    with LabTracker(
        base_url="http://testserver",
        default_project_id=PROJECT_DEFAULT,
        transport=httpx.MockTransport(handler),
    ) as lt:
        result = savefig(FakeFigure(), repo / "plot.png", client=lt)

    assert result.action == "imported"
    assert uploaded_to == [PROJECT_CHECKOUT]


def test_live_capture_outside_a_bound_checkout_falls_back_to_the_client_default(
    tmp_path: Path,
) -> None:
    uploaded_to: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        uploaded_to.append(_multipart_field(request.content, "project_id"))
        return httpx.Response(201, json={"data": {"note_id": "note-1", "metadata": {}}})

    with LabTracker(
        base_url="http://testserver",
        default_project_id=PROJECT_DEFAULT,
        transport=httpx.MockTransport(handler),
    ) as lt:
        savefig(FakeFigure(), tmp_path / "plot.png", client=lt)

    assert uploaded_to == [PROJECT_DEFAULT]


def test_profile_client_is_pointed_at_the_checkout_project(tmp_path: Path) -> None:
    _write_profile(tmp_path, default_project_id=PROJECT_DEFAULT)
    repo = _git_init(tmp_path / "repo")
    _bind_checkout(repo, PROJECT_CHECKOUT)

    project = resolve_capture_project(repo / "plot.png", project_id=None)
    client, project_id, should_close = figure_module._resolve_capture_client(
        client=None, project_id=project.project_id if project else None
    )

    assert client is not None
    assert project_id == PROJECT_CHECKOUT
    assert client.default_project_id == PROJECT_CHECKOUT
    assert should_close is True
    client.close()


def test_offline_queue_in_a_checkout_bound_to_b_names_b_not_the_profile_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_profile(tmp_path, default_project_id=PROJECT_DEFAULT)
    repo = _git_init(tmp_path / "repo")
    _bind_checkout(repo, PROJECT_CHECKOUT)
    monkeypatch.chdir(repo)

    result = savefig(FakeFigure(), repo / "out.png")

    assert result.action == "queued"
    event = read_event(Path(result.queued_event))
    assert Path(result.queued_event).parent == repo / ".lab-tracker" / "outbox" / "watch"
    assert event["context"]["project_id"] == PROJECT_CHECKOUT


def test_offline_queue_uses_the_watch_config_before_the_profile_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_profile(tmp_path, default_project_id=PROJECT_DEFAULT)
    repo = _git_init(tmp_path / "repo")
    _bind_watch_config(repo, PROJECT_WATCH)
    monkeypatch.chdir(repo)

    result = savefig(FakeFigure(), repo / "out.png")

    assert result.action == "queued"
    assert read_event(Path(result.queued_event))["context"]["project_id"] == PROJECT_WATCH


def test_offline_queue_uses_the_profile_default_only_when_nothing_else_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_profile(tmp_path, default_project_id=PROJECT_DEFAULT)
    repo = _git_init(tmp_path / "repo")
    monkeypatch.chdir(repo)

    result = savefig(FakeFigure(), repo / "out.png")

    assert result.action == "queued"
    assert read_event(Path(result.queued_event))["context"]["project_id"] == PROJECT_DEFAULT
