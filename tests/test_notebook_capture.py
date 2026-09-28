"""Jupyter notebook saves as daily staged lab-notebook pages."""

from __future__ import annotations

import hashlib
import json
import subprocess
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

import lab_tracker_client.notebook_capture as notebook_module
from lab_tracker_client import LabTracker
from lab_tracker_client import cli as lt_cli
from lab_tracker_client.watch import (
    NOT_DUE_REASON,
    list_event_files,
    read_event,
    sync_outbox_path,
)

TZ = timezone(timedelta(hours=2))
MORNING = datetime(2026, 9, 28, 9, 0, tzinfo=TZ)
SECRET_CODE = "api_secret = 'do-not-copy-me'"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for key in (
        "LAB_TRACKER_AUTOTRACK",
        "LAB_TRACKER_PROJECT_ID",
        "LAB_TRACKER_SESSION_ID",
        "LAB_TRACKER_WATCH_OUTBOX",
        "LAB_TRACKER_WATCH_CONFIG",
        "LAB_TRACKER_SESSION_CONTEXT",
        "JUPYTER_CONFIG_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-config"))
    monkeypatch.setenv("JUPYTER_CONFIG_DIR", str(tmp_path / "jupyter"))
    notebook_module._reset_notebook_capture_state_for_tests()
    yield
    notebook_module._reset_notebook_capture_state_for_tests()


def _git_checkout(path: Path, project_id: str | None = "project-notebook") -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    if project_id:
        (path / "lt_ids.json").write_text(json.dumps({"project_id": project_id}))
    return path


def _notebook(
    path: Path, *, extra_markdown: str = "", extra_cells: list[Any] | None = None
) -> Path:
    cells: list[dict[str, Any]] = [
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "# Dose response\n",
                "Data from https://user:hunter2@data.example.org/run?token=abc on rig 2.\n",
                "![plot](data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==)",
                extra_markdown,
            ],
        },
        {
            "cell_type": "code",
            "execution_count": 3,
            "metadata": {},
            "source": [
                "import numpy as np\n",
                "from scipy.stats import linregress\n",
                f"{SECRET_CODE}\n",
                "def fit(x):\n",
                "    return linregress(x, x)\n",
            ],
            "outputs": [
                {"output_type": "display_data", "data": {"image/png": "iVBOR"}, "metadata": {}},
                {
                    "output_type": "error",
                    "ename": "ValueError",
                    "evalue": "token=leaked-secret",
                    "traceback": [],
                },
            ],
        },
        {"cell_type": "code", "execution_count": None, "metadata": {}, "source": "", "outputs": []},
        {"cell_type": "raw", "metadata": {}, "source": "raw text"},
        *(extra_cells or []),
    ]
    payload = {
        "cells": cells,
        "metadata": {
            "kernelspec": {"name": "python3", "display_name": "Python 3 (ipykernel)"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _outbox(checkout: Path) -> Path:
    return checkout / ".lab-tracker" / "outbox" / "watch"


def test_a_bound_save_queues_one_bounded_page_with_a_pointer_not_the_file(
    tmp_path: Path,
) -> None:
    checkout = _git_checkout(tmp_path / "analysis")
    notebook = _notebook(checkout / "notebooks" / "dose.ipynb")

    result = notebook_module.record_notebook_save(notebook, now=MORNING)

    assert result["action"] == "queued"
    assert result["project_id"] == "project-notebook"
    assert result["deliver_after"] == "2026-09-28T22:00:00+00:00"  # next local midnight
    event = read_event(Path(result["event_path"]))
    assert Path(result["event_path"]).parent == _outbox(checkout).resolve()
    digest = hashlib.sha256(notebook.read_bytes()).hexdigest()
    assert event["capture_kind"] == "notebook"
    assert event["sink"] == "staged-note"
    assert event["adapter"] == "lab-tracker-client-notebook"
    assert event["context"]["project_id"] == "project-notebook"
    source = event["source"]
    assert "path" not in source  # a sync must never upload the notebook itself
    assert source["uri"] == notebook.resolve().as_uri()
    assert source["relative_path"] == "notebooks/dose.ipynb"
    assert source["content_hash"] == digest
    assert source["external_id"] == "notebook:notebooks/dose.ipynb@2026-09-28"
    assert event["artifacts"] == [
        {
            "title": "dose.ipynb",
            "kind": "notebook",
            "uri": notebook.resolve().as_uri(),
            "content_hash": digest,
            "size_bytes": notebook.stat().st_size,
            "summary": "Jupyter notebook; the file stays in the checkout.",
        }
    ]
    payload = event["payload"]
    assert payload["status"] == "staged"
    assert payload["local_day"] == "2026-09-28"
    assert payload["metadata"] == {
        "notebook_path": "notebooks/dose.ipynb",
        "notebook_sha256": digest,
        "notebook_size_bytes": notebook.stat().st_size,
        "notebook_local_day": "2026-09-28",
        "notebook_saved_at": "2026-09-28T09:00:00+02:00",
        "notebook_page": True,
        "notebook_kernel": "python3",
        "notebook_language": "python",
        "notebook_cell_count": 4,
        "notebook_code_cell_count": 2,
        "notebook_markdown_cell_count": 1,
        "notebook_markdown_truncated": False,
    }
    body = payload["body"]
    assert f"SHA-256: `{digest}`" in body
    assert "Kernel: python3 (Python 3 (ipykernel)); language python" in body
    assert "Cells: 4 (2 code, 1 executed; 1 markdown; 1 raw)" in body
    assert "# Dose response" in body
    # Markdown keeps its text but not URL credentials, queries, or embedded images.
    assert "https://data.example.org/run on rig 2." in body
    assert "hunter2" not in body and "token=abc" not in body
    assert "iVBORw0KGgo" not in body
    # Code cells are summarized, never copied; error messages are not copied.
    assert (
        "- Cell 2 · In [3] · 5 lines · imports numpy, scipy · defines fit · "
        "outputs 1 image, error ValueError"
    ) in body
    assert "- Cell 3 · not run · 0 lines" in body
    assert SECRET_CODE not in body and "do-not-copy-me" not in body
    assert "leaked-secret" not in body


def test_autosaves_coalesce_into_one_pending_page_per_local_day(tmp_path: Path) -> None:
    checkout = _git_checkout(tmp_path / "analysis")
    notebook = _notebook(checkout / "dose.ipynb")
    record = notebook_module.record_notebook_save

    first = record(notebook, now=MORNING)
    assert record(notebook, now=MORNING + timedelta(minutes=2))["action"] == "unchanged"
    _notebook(notebook, extra_markdown="\nAfternoon: the fit converged.")
    replaced = record(notebook, now=MORNING + timedelta(hours=6))
    assert replaced["action"] == "replaced"
    assert replaced["event_path"] == first["event_path"]
    assert len(list_event_files(_outbox(checkout))) == 1
    event = read_event(Path(first["event_path"]))
    assert "the fit converged" in event["payload"]["body"]
    assert event["payload"]["metadata"]["notebook_saved_at"] == "2026-09-28T15:00:00+02:00"
    assert event["sync"]["status"] == "pending"

    # Once the day's page is delivered, later saves that day add nothing ...
    delivered = read_event(Path(first["event_path"]))
    delivered["sync"] = {"status": "synced", "attempts": 1, "note_id": "note-day-1"}
    Path(first["event_path"]).write_text(json.dumps(delivered), encoding="utf-8")
    _notebook(notebook, extra_markdown="\nLate edit.")
    assert record(notebook, now=MORNING + timedelta(hours=10))["action"] == "already_synced"
    # ... and the next day's first save starts a new page.
    next_day = record(notebook, now=MORNING + timedelta(days=1))
    assert next_day["action"] == "queued"
    assert next_day["event_path"] != first["event_path"]
    assert len(list_event_files(_outbox(checkout))) == 2


def _sync_client(uploads: list[dict[str, Any]]) -> LabTracker:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/notes":
            return httpx.Response(
                200, json={"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        body = request.content
        fields = {}
        for name in ("project_id", "metadata", "status"):
            chunk = body.split(f'name="{name}"'.encode(), 1)[1].split(b"\r\n\r\n", 1)[1]
            fields[name] = chunk.split(b"\r\n--", 1)[0].decode()
        uploads.append({**fields, "body": body})
        return httpx.Response(201, json={"data": {"note_id": f"note-{len(uploads)}"}})

    return LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler))


def test_sync_holds_a_page_until_its_local_day_is_over(tmp_path: Path) -> None:
    checkout = _git_checkout(tmp_path / "analysis")
    notebook = _notebook(checkout / "dose.ipynb")
    today = notebook_module.record_notebook_save(notebook)
    past = notebook_module.record_notebook_save(
        notebook, now=datetime(2000, 1, 1, 12, 0, tzinfo=TZ)
    )
    uploads: list[dict[str, Any]] = []

    with _sync_client(uploads) as lt:
        summary = sync_outbox_path(lt, _outbox(checkout))

    by_path = {result["path"]: result for result in summary["results"]}
    assert by_path[today["event_path"]]["action"] == "skipped"
    assert by_path[today["event_path"]]["reason"] == NOT_DUE_REASON
    assert by_path[past["event_path"]]["action"] == "imported"
    assert len(uploads) == 1
    upload = uploads[0]
    assert upload["project_id"] == "project-notebook"
    assert upload["status"] == "staged"
    assert b"# Notebook page: dose.ipynb (2000-01-01)" in upload["body"]
    assert b'"nbformat"' not in upload["body"]  # the page, not the notebook file
    metadata = json.loads(upload["metadata"])
    assert metadata["evidence_capture_kind"] == "notebook"
    assert metadata["evidence_source_provider"] == "jupyter-notebook"
    assert metadata["notebook_page"] is True
    assert metadata["notebook_local_day"] == "2000-01-01"
    assert read_event(Path(today["event_path"]))["sync"]["status"] == "pending"


def test_unbound_notebooks_are_skipped_with_one_notice_per_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unbound = _git_checkout(tmp_path / "unbound", project_id=None)
    notebook = _notebook(unbound / "a.ipynb")
    other = _notebook(unbound / "sub" / "b.ipynb")
    for path in (notebook, other, notebook):
        assert notebook_module.record_notebook_save(path, now=MORNING)["action"] == "skipped"
    err = capsys.readouterr().err
    assert err.count("not capturing notebook saves") == 1
    assert "lt project bind" in err
    assert not (unbound / ".lab-tracker").exists()

    loose = _notebook(tmp_path / "loose" / "c.ipynb")
    assert notebook_module.record_notebook_save(loose, now=MORNING)["action"] == "skipped"
    assert "Set LAB_TRACKER_PROJECT_ID for the Jupyter server" in capsys.readouterr().err

    monkeypatch.setenv("LAB_TRACKER_PROJECT_ID", "project-env")
    queued = notebook_module.record_notebook_save(loose, now=MORNING)
    assert queued["action"] == "queued"
    assert queued["project_id"] == "project-env"
    assert Path(queued["event_path"]).parent == _outbox(tmp_path / "loose").resolve()


def test_post_save_hook_never_breaks_a_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = _git_checkout(tmp_path / "analysis")
    notebook = _notebook(checkout / "dose.ipynb")
    hook = notebook_module.post_save_hook

    hook(model={"type": "notebook"}, os_path=str(notebook), contents_manager=object())
    assert len(list_event_files(_outbox(checkout))) == 1

    # Not a notebook, not a notebook model, or no path: ignored.
    (checkout / "notes.txt").write_text("hi")
    hook(model={"type": "file"}, os_path=str(checkout / "notes.txt"), contents_manager=None)
    hook(model={"type": "file"}, os_path=str(notebook), contents_manager=None)
    hook(model=None, os_path=None, contents_manager=None)
    assert len(list_event_files(_outbox(checkout))) == 1

    # A vanished file or a broken outbox is reported once, never raised.
    hook(model={"type": "notebook"}, os_path=str(checkout / "gone.ipynb"))
    hook(model={"type": "notebook"}, os_path=str(checkout / "gone.ipynb"))
    assert capsys.readouterr().err.count("could not record the notebook save") == 1

    # Invalid JSON still gets a pointer page.
    broken = checkout / "broken.ipynb"
    broken.write_text("{not json")
    result = notebook_module.record_notebook_save(broken, now=MORNING)
    body = read_event(Path(result["event_path"]))["payload"]["body"]
    assert "_Not summarized: the file is not valid notebook JSON._" in body

    monkeypatch.setenv("LAB_TRACKER_AUTOTRACK", "0")
    other = _notebook(checkout / "other.ipynb")
    hook(model={"type": "notebook"}, os_path=str(other))
    assert len(list_event_files(_outbox(checkout))) == 2


def test_page_text_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _git_checkout(tmp_path / "analysis")
    many_cells = [
        {"cell_type": "code", "execution_count": i, "source": "x = 1", "outputs": []}
        for i in range(notebook_module.NOTEBOOK_CODE_CELLS_MAX + 5)
    ]
    notebook = _notebook(
        checkout / "long.ipynb",
        extra_markdown="word " * notebook_module.NOTEBOOK_MARKDOWN_MAX_CHARS,
        extra_cells=many_cells,
    )
    event = read_event(
        Path(notebook_module.record_notebook_save(notebook, now=MORNING)["event_path"])
    )
    body = event["payload"]["body"]
    assert "more characters omitted]_" in body
    assert "- _7 more code cells not listed_" in body  # 2 base + 205 extra - 200
    assert event["payload"]["metadata"]["notebook_markdown_truncated"] is True
    assert len(body) < notebook_module.NOTEBOOK_MARKDOWN_MAX_CHARS + 60_000

    monkeypatch.setattr(notebook_module, "NOTEBOOK_PARSE_MAX_BYTES", 10)
    huge = read_event(
        Path(
            notebook_module.record_notebook_save(notebook, now=MORNING + timedelta(days=1))[
                "event_path"
            ]
        )
    )
    assert "_Not summarized: the notebook is larger than 10 bytes._" in huge["payload"]["body"]
    assert huge["source"]["content_hash"] == hashlib.sha256(notebook.read_bytes()).hexdigest()


# --- Jupyter Server extension ---------------------------------------------------


class _Log:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str, *args: Any) -> None:
        self.messages.append(message % args)

    def warning(self, message: str, *args: Any) -> None:
        self.messages.append("WARNING " + message % args)


class _ContentsManager:
    """jupyter_server 2.x: one configurable hook plus registered ones."""

    def __init__(self, configured: Any = None) -> None:
        self.post_save_hook = configured
        self._post_save_hooks: list[Any] = []

    def register_post_save_hook(self, hook: Any) -> None:
        self._post_save_hooks.append(hook)

    def run_post_save_hooks(self, model: Any, os_path: str) -> None:
        hooks = [self.post_save_hook] if self.post_save_hook is not None else []
        for hook in [*hooks, *self._post_save_hooks]:
            hook(os_path=os_path, model=model, contents_manager=self)


def test_the_extension_registers_the_hook_next_to_an_existing_one(tmp_path: Path) -> None:
    assert notebook_module._jupyter_server_extension_points() == [
        {"module": "lab_tracker_client.notebook_capture"}
    ]
    seen: list[str] = []

    def theirs(*, os_path: str, **_kwargs: Any) -> None:
        seen.append(os_path)

    manager = _ContentsManager(configured=theirs)
    app = types.SimpleNamespace(contents_manager=manager, log=_Log())
    notebook_module._load_jupyter_server_extension(app)
    notebook_module._load_jupyter_server_extension(app)
    assert manager.post_save_hook is theirs
    assert manager._post_save_hooks == [notebook_module.post_save_hook]
    assert app.log.messages[-1].endswith("already_registered.")

    checkout = _git_checkout(tmp_path / "analysis")
    notebook = _notebook(checkout / "dose.ipynb")
    manager.run_post_save_hooks({"type": "notebook"}, str(notebook))
    assert seen == [str(notebook)]
    assert len(list_event_files(_outbox(checkout))) == 1

    # Configured by hand as the import string: nothing is added.
    configured = _ContentsManager(configured=notebook_module.post_save_hook)
    assert notebook_module.register_post_save_hook(configured) == "configured"
    assert configured._post_save_hooks == []


def test_an_older_server_never_has_its_hook_replaced() -> None:
    def theirs(**_kwargs: Any) -> None:
        return None

    old_with_hook = types.SimpleNamespace(post_save_hook=theirs)
    app = types.SimpleNamespace(contents_manager=old_with_hook, log=_Log())
    notebook_module._load_jupyter_server_extension(app)
    assert old_with_hook.post_save_hook is theirs
    assert "never replaces" in app.log.messages[-1]

    old_without_hook = types.SimpleNamespace(post_save_hook=None)
    assert notebook_module.register_post_save_hook(old_without_hook) == "installed"
    assert old_without_hook.post_save_hook is notebook_module.post_save_hook

    broken = types.SimpleNamespace(log=_Log())  # no contents_manager at all
    notebook_module._load_jupyter_server_extension(broken)
    assert "was not registered" in broken.log.messages[-1]


# --- `lt setup autotrack --jupyter` ---------------------------------------------


def test_setup_autotrack_jupyter_manages_the_server_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("IPYTHONDIR", str(tmp_path / "ipython"))
    config_file = tmp_path / "jupyter" / "jupyter_server_config.d" / "lab-tracker.json"
    assert notebook_module.jupyter_hook_config_path() == config_file

    with pytest.raises(SystemExit, match="--yes"):
        lt_cli.main(["setup", "autotrack", "--jupyter"])
    lt_cli.main(["setup", "autotrack", "--jupyter", "--dry-run"])
    preview = json.loads(capsys.readouterr().out)
    assert (preview["target"], preview["action"]) == ("jupyter", "would-install")
    assert preview["hook"] == "lab_tracker_client.notebook_capture.post_save_hook"
    assert preview["content"] == notebook_module.jupyter_hook_source()
    assert not config_file.exists()

    lt_cli.main(["setup", "autotrack", "--jupyter", "--yes"])
    installed = json.loads(capsys.readouterr().out)
    assert installed["action"] == "installed"
    assert installed["restart_required"] is True
    assert json.loads(config_file.read_text()) == {
        "ServerApp": {"jpserver_extensions": {"lab_tracker_client.notebook_capture": True}}
    }
    assert not (tmp_path / "ipython").exists()  # the IPython startup file is separate
    lt_cli.main(["setup", "autotrack", "--jupyter", "--yes"])
    assert json.loads(capsys.readouterr().out)["action"] == "current"

    lt_cli.main(["setup", "autotrack", "--jupyter", "--yes", "--uninstall"])
    assert json.loads(capsys.readouterr().out)["action"] == "removed"
    assert not config_file.exists()
    lt_cli.main(["setup", "autotrack", "--jupyter", "--yes", "--uninstall"])
    assert json.loads(capsys.readouterr().out)["action"] == "absent"


def test_setup_autotrack_jupyter_reports_and_keeps_other_hooks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jupyter = tmp_path / "jupyter"
    jupyter.mkdir()
    (jupyter / "jupyter_server_config.json").write_text(
        json.dumps({"FileContentsManager": {"post_save_hook": "nbstrip.hook"}})
    )
    (jupyter / "jupyter_server_config.py").write_text(
        "c = get_config()\n"
        "c.FileContentsManager.post_save_hook = 'scripts.export'\n"
        "# c.ContentsManager.post_save_hook = 'commented.out'\n"
    )
    lt_cli.main(["setup", "autotrack", "--jupyter", "--yes"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "installed"
    assert payload["other_post_save_hooks"] == [
        {"file": str(jupyter / "jupyter_server_config.json"), "value": "nbstrip.hook"},
        {"file": str(jupyter / "jupyter_server_config.py"), "value": "scripts.export"},
    ]
    assert "kept" in payload["note"]
    assert json.loads((jupyter / "jupyter_server_config.json").read_text()) == {
        "FileContentsManager": {"post_save_hook": "nbstrip.hook"}
    }


def test_setup_autotrack_refuses_a_foreign_file_before_writing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import lab_tracker_client.script_capture as script_module

    site = tmp_path / "site-packages"
    site.mkdir()
    monkeypatch.setattr(script_module, "scripts_site_dir", lambda: site)
    foreign = tmp_path / "jupyter" / "jupyter_server_config.d" / "lab-tracker.json"
    foreign.parent.mkdir(parents=True)
    foreign.write_text('{"ServerApp": {"jpserver_extensions": {"someone_else": true}}}')

    with pytest.raises(SystemExit, match="not written by Lab Tracker"):
        lt_cli.main(["setup", "autotrack", "--jupyter", "--scripts", "--yes"])
    assert not (site / script_module.SCRIPTS_PTH_FILENAME).exists()
    assert "someone_else" in foreign.read_text()

    foreign.unlink()
    lt_cli.main(["setup", "autotrack", "--jupyter", "--scripts", "--dry-run"])
    combined = json.loads(capsys.readouterr().out)
    assert [item["target"] for item in combined["targets"]] == ["jupyter", "scripts"]
    assert {item["action"] for item in combined["targets"]} == {"would-install"}


def test_setup_status_reports_the_jupyter_hook_and_scripts_pth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lab_tracker_client.script_capture as script_module
    from lab_tracker_client.setup import _autotrack_status

    site = tmp_path / "site-packages"
    site.mkdir()
    monkeypatch.setattr(script_module, "scripts_site_dir", lambda: site)
    monkeypatch.setenv("IPYTHONDIR", str(tmp_path / "ipython"))
    status = _autotrack_status()
    assert status["installed"] is False  # the IPython startup file, as before
    assert status["jupyter"]["installed"] is False
    assert status["scripts"]["installed"] is False

    notebook_module.install_jupyter_hook()
    script_module.install_scripts_pth()
    status = _autotrack_status()
    assert status["jupyter"]["installed"] is True
    assert status["jupyter"]["up_to_date"] is True
    assert status["scripts"]["installed"] is True
    assert status["scripts"]["pth_file"] == str(site / script_module.SCRIPTS_PTH_FILENAME)
