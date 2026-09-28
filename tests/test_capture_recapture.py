"""Re-capturing under the same capture id coalesces against the real server.

The server refuses a capture-id replay whose fields differ (every capture
stamps a fresh observed-at time), so the client must find the note that id
already made and coalesce into it rather than report a failure.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import lab_tracker_client.figure as figure_module
from lab_tracker_client import LabTracker, autotrack, savefig
from lab_tracker_client.figure import _reset_figure_capture_state_for_tests, capture_figure_bytes

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class FakeFigure:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def savefig(self, path: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        Path(path).write_bytes(self.data)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for key in (
        "LAB_TRACKER_PROJECT_ID",
        "LAB_TRACKER_SESSION_ID",
        "LAB_TRACKER_WATCH_OUTBOX",
        "LAB_TRACKER_SESSION_CONTEXT",
        "LAB_TRACKER_AUTOTRACK",
        "JPY_SESSION_NAME",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    _reset_figure_capture_state_for_tests()
    yield
    _reset_figure_capture_state_for_tests()


def _project(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    return str(response.json()["data"]["project_id"])


def _lab_tracker(client: TestClient, headers: dict[str, str], project_id: str) -> LabTracker:
    return LabTracker(
        base_url="http://testserver",
        access_token=headers["Authorization"].split()[1],
        transport=client._transport,
        default_project_id=project_id,
    )


def _notes(client: TestClient, headers: dict[str, str], project_id: str) -> list[dict[str, Any]]:
    response = client.get("/notes", params={"project_id": project_id}, headers=headers)
    return list(response.json()["data"])


def test_resaving_a_path_coalesces_into_its_first_note(
    client: TestClient, admin_auth_headers: dict[str, str], tmp_path: Path
) -> None:
    project_id = _project(client, admin_auth_headers, "resave")
    target = tmp_path / "plot.png"
    with _lab_tracker(client, admin_auth_headers, project_id) as lt:
        first = savefig(FakeFigure(PNG), target, client=lt, project_id=project_id)
        second = savefig(FakeFigure(PNG), target, client=lt, project_id=project_id)
        # A new process knows nothing of the first note and must look it up.
        figure_module._CAPTURE_NOTE_IDS.clear()
        third = savefig(FakeFigure(PNG + b"1"), target, client=lt, project_id=project_id)

    assert first.action == "imported"
    assert (second.action, second.stale_review_bytes, second.errors) == ("coalesced", False, [])
    assert (third.action, third.stale_review_bytes, third.errors) == ("coalesced", True, [])
    notes = _notes(client, admin_auth_headers, project_id)
    assert len(notes) == 1
    assert first.note is not None and third.note is not None
    assert notes[0]["note_id"] == first.note["note_id"] == third.note["note_id"]
    metadata = notes[0]["metadata"]
    assert str(metadata["figure_review_bytes_stale"]) == "True"  # stored as text
    assert metadata["figure_content_hash_current"] == third.content_hash
    assert metadata["evidence_content_hash"] == first.content_hash  # first bytes stay


def test_rerunning_a_notebook_cell_coalesces_its_displayed_figure(
    client: TestClient, admin_auth_headers: dict[str, str], tmp_path: Path, monkeypatch
) -> None:
    from test_display_capture import FakeFigure as DisplayedFigure
    from test_display_capture import FakeShell, _plotting_cell

    checkout = tmp_path / "analysis"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)  # noqa: S603, S607
    project_id = _project(client, admin_auth_headers, "notebook")
    (checkout / "lt_ids.json").write_text(json.dumps({"project_id": project_id}))
    monkeypatch.setenv("JPY_SESSION_NAME", str(checkout / "nb.ipynb"))
    shell = FakeShell()
    ipython = types.ModuleType("IPython")
    ipython.get_ipython = lambda: shell  # type: ignore[attr-defined]
    matplotlib = types.ModuleType("matplotlib")
    figure = types.ModuleType("matplotlib.figure")
    figure.Figure = DisplayedFigure  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "IPython", ipython)
    monkeypatch.setitem(sys.modules, "matplotlib", matplotlib)
    monkeypatch.setitem(sys.modules, "matplotlib.figure", figure)
    results: list[Any] = []
    real_capture = figure_module.capture_figure_bytes
    monkeypatch.setattr(
        figure_module,
        "capture_figure_bytes",
        lambda *args, **kwargs: results.append(real_capture(*args, **kwargs)) or results[-1],
    )

    with _lab_tracker(client, admin_auth_headers, project_id) as lt:
        try:
            assert autotrack(client=lt) is True
            for png in (PNG, PNG, PNG + b"-edited"):
                shell.run_cell("plt.plot(x)", _plotting_cell([DisplayedFigure(png)]))
        finally:
            autotrack(False)

    assert [result.action for result in results] == ["imported", "coalesced", "coalesced"]
    assert [result.stale_review_bytes for result in results] == [False, False, True]
    assert len(_notes(client, admin_auth_headers, project_id)) == 1


def test_bytes_captures_with_distinct_logical_ids_stay_distinct(
    client: TestClient, admin_auth_headers: dict[str, str], tmp_path: Path
) -> None:
    project_id = _project(client, admin_auth_headers, "distinct")
    with _lab_tracker(client, admin_auth_headers, project_id) as lt:
        for index in (1, 2, 1):
            result = capture_figure_bytes(
                PNG,
                filename=f"fig{index}.png",
                anchor=tmp_path,
                source_uri=f"{tmp_path.as_uri()}#display=cell-a/figure-{index}",
                logical_id=f"display/nb/cell-a/figure-{index}",
                client=lt,
                project_id=project_id,
            )
            assert result.action in {"imported", "coalesced"}
    assert len(_notes(client, admin_auth_headers, project_id)) == 2


class _Recording(httpx.BaseTransport):
    """Delegates to the app while recording every request's method and path."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self.inner = inner
        self.requests: list[tuple[str, str]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        return self.inner.handle_request(request)


def _curate(client: TestClient, headers: dict[str, str], note_id: str, curation: str) -> None:
    if curation == "committed":
        response = client.patch(f"/notes/{note_id}", json={"status": "committed"}, headers=headers)
    else:
        response = client.post(f"/notes/{note_id}/archive", json={}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == curation


@pytest.mark.parametrize("curation", ["committed", "archived"])
def test_a_curated_note_is_never_rewritten_by_a_capture(
    client: TestClient, admin_auth_headers: dict[str, str], tmp_path: Path, curation: str
) -> None:
    """A person's commit or archive is final for automatic capture: unchanged
    bytes coalesce with no write, and new bytes become a new staged note that
    names the curated one, which stays exactly as the person left it."""

    project_id = _project(client, admin_auth_headers, f"curated-{curation}")
    target = tmp_path / "plot.png"
    recorder = _Recording(client._transport)
    lt = LabTracker(
        base_url="http://testserver",
        access_token=admin_auth_headers["Authorization"].split()[1],
        transport=recorder,
        default_project_id=project_id,
    )
    with lt:
        first = savefig(FakeFigure(PNG), target, client=lt, project_id=project_id)
        assert first.note is not None
        curated_id = str(first.note["note_id"])
        _curate(client, admin_auth_headers, curated_id, curation)
        before = client.get(f"/notes/{curated_id}", headers=admin_auth_headers).json()["data"]
        recorder.requests.clear()

        unchanged = savefig(FakeFigure(PNG), target, client=lt, project_id=project_id)
        figure_module._CAPTURE_NOTE_IDS.clear()  # a new process looks the note up
        changed = savefig(FakeFigure(PNG + b"new"), target, client=lt, project_id=project_id)
        again = savefig(FakeFigure(PNG + b"new"), target, client=lt, project_id=project_id)

    assert (unchanged.action, unchanged.errors) == ("coalesced", [])
    assert unchanged.note is not None and str(unchanged.note["note_id"]) == curated_id
    assert (changed.action, changed.reason, changed.errors) == (
        "imported",
        figure_module.CAPTURE_REVISION_REASON,
        [],
    )
    assert changed.client_capture_id == f"figure:plot.png:{changed.content_hash[:12]}"
    assert (again.action, again.errors) == ("coalesced", [])
    assert changed.note is not None and again.note is not None
    assert again.note["note_id"] == changed.note["note_id"] != curated_id
    # The capture never wrote the curated note.
    assert not [request for request in recorder.requests if request[0] == "PATCH"]
    after = client.get(f"/notes/{curated_id}", headers=admin_auth_headers).json()["data"]
    assert after == before
    notes = {note["note_id"]: note for note in _notes(client, admin_auth_headers, project_id)}
    assert set(notes) == {curated_id, changed.note["note_id"]}
    revision = notes[changed.note["note_id"]]
    assert revision["status"] == "staged"
    assert revision["client_capture_id"] == changed.client_capture_id
    assert revision["metadata"]["supersedes_capture_note_id"] == curated_id
    assert revision["metadata"]["supersedes_capture_note_status"] == curation
    assert revision["metadata"]["evidence_content_hash"] == changed.content_hash


def test_a_replayed_curated_note_is_left_alone_on_the_200_path(tmp_path: Path) -> None:
    """The exact-replay answer (200) for a committed note takes the same rule."""

    import hashlib

    requests: list[tuple[str, str, str]] = []
    old_hash = hashlib.sha256(PNG).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        capture_id = ""
        if b'name="client_capture_id"' in body:
            chunk = body.split(b'name="client_capture_id"', 1)[1].split(b"\r\n\r\n", 1)[1]
            capture_id = chunk.split(b"\r\n--", 1)[0].decode()
        requests.append((request.method, request.url.path, capture_id))
        if request.method == "POST" and capture_id == "figure:plot.png":
            curated = {
                "note_id": "note-curated",
                "status": "committed",
                "client_capture_id": capture_id,
                "metadata": {"evidence_content_hash": old_hash},
            }
            return httpx.Response(200, json={"data": curated})
        if request.method == "POST":
            return httpx.Response(
                201, json={"data": {"note_id": "note-revision", "status": "staged"}}
            )
        return httpx.Response(500, json={"error": {"message": "unexpected"}})

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt:
        unchanged = savefig(FakeFigure(PNG), tmp_path / "plot.png", client=lt)
        changed = savefig(FakeFigure(PNG + b"new"), tmp_path / "plot.png", client=lt)

    assert unchanged.action == "coalesced"
    assert changed.action == "imported"
    assert changed.reason == figure_module.CAPTURE_REVISION_REASON
    assert [request[0] for request in requests] == ["POST", "POST", "POST"]
    assert requests[2][2] == f"figure:plot.png:{changed.content_hash[:12]}"
    assert changed.metadata["supersedes_capture_note_id"] == "note-curated"
