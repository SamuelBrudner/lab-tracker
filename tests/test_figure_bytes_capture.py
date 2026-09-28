"""In-memory figure capture: bytes that were displayed or shown, never saved."""

from __future__ import annotations

import json
import subprocess
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

import lab_tracker_client.figure as figure_module
from lab_tracker_client import LabTracker
from lab_tracker_client.figure import (
    QUEUED_PAYLOAD_DIRNAME,
    _reset_figure_capture_state_for_tests,
    capture_figure_bytes,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"inline-figure"


def _multipart_field(body: bytes, name: str) -> str:
    marker = f'name="{name}"'.encode()
    assert marker in body
    chunk = body.split(marker, 1)[1].split(b"\r\n\r\n", 1)[1]
    return chunk.split(b"\r\n--", 1)[0].decode("utf-8")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _reset_figure_capture_state_for_tests()
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-config"))
    for key in (
        "LAB_TRACKER_PROJECT_ID",
        "LAB_TRACKER_BASE_URL",
        "LAB_TRACKER_MCP_BASE_URL",
        "LAB_TRACKER_ACCESS_TOKEN",
        "LAB_TRACKER_SESSION_ID",
        "LAB_TRACKER_WATCH_OUTBOX",
        "LAB_TRACKER_WATCH_CONFIG",
        "LAB_TRACKER_CAPTURE_OUTBOX",
        "LAB_TRACKER_SESSION_CONTEXT",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    _reset_figure_capture_state_for_tests()


def _bound_checkout(path: Path, project_id: str = "project-bound") -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    (path / "lt_ids.json").write_text(json.dumps({"project_id": project_id}))
    return path


def _files_under(root: Path) -> set[Path]:
    return {
        item.relative_to(root)
        for item in root.rglob("*")
        if item.is_file() and ".git" not in item.relative_to(root).parts
    }


def test_bytes_capture_uploads_the_displayed_bytes_without_writing_a_file(
    tmp_path: Path,
) -> None:
    checkout = _bound_checkout(tmp_path / "analysis")
    before = _files_under(checkout)
    uploads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        assert PNG in body
        uploads.append(
            {
                "project_id": _multipart_field(body, "project_id"),
                "client_capture_id": _multipart_field(body, "client_capture_id"),
                "metadata": json.loads(_multipart_field(body, "metadata")),
            }
        )
        return httpx.Response(201, json={"data": {"note_id": "note-1", "metadata": {}}})

    source_uri = (checkout / "nb.ipynb").as_uri() + "#display=cell-1/figure-1"
    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        result = capture_figure_bytes(
            PNG,
            filename="nb-cell3-figure1.png",
            anchor=checkout,
            source_uri=source_uri,
            logical_id="display/nb.ipynb/cell-1/figure-1",
            client=lt,
            metadata={"figure_display_captured": True},
            require_bound_project=True,
        )

    assert result.action == "imported"
    assert result.client_capture_id == "figure:display/nb.ipynb/cell-1/figure-1"
    assert uploads[0]["project_id"] == "project-bound"
    metadata = uploads[0]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["evidence_source_uri"] == source_uri
    assert metadata["evidence_title"] == "nb-cell3-figure1.png"
    assert metadata["evidence_content_hash"] == sha256(PNG).hexdigest()
    assert metadata["figure_display_captured"] is True
    assert metadata["figure_full_size_bytes"] == len(PNG)
    # A live capture of in-memory bytes leaves the user's folders untouched.
    assert _files_under(checkout) == before


def test_bytes_capture_follows_the_bound_project_rule(tmp_path: Path, capsys) -> None:
    loose = tmp_path / "loose"
    loose.mkdir()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-default",
        transport=httpx.MockTransport(handler),
    ) as lt:
        result = capture_figure_bytes(
            PNG,
            filename="fig.png",
            anchor=loose,
            source_uri=loose.as_uri() + "#display=cell-x/figure-1",
            logical_id="display/unknown/cell-x/figure-1",
            client=lt,
            require_bound_project=True,
        )

    assert result.action == "skipped"
    assert result.reason == figure_module.AUTOTRACK_UNBOUND_REASON
    assert calls == []
    assert list(loose.iterdir()) == []
    assert "Nothing was sent or queued" in capsys.readouterr().err


def test_offline_bytes_capture_keeps_the_bytes_in_the_outbox_and_syncs_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the server down, the only file written is the outbox copy of the
    bytes; the ordinary sync delivers it under the same capture id and URI."""

    from lab_tracker_client.watch import read_event, sync_outbox_path

    checkout = _bound_checkout(tmp_path / "analysis")
    monkeypatch.chdir(checkout)

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    source_uri = (checkout / "nb.ipynb").as_uri() + "#display=cell-1/figure-1"
    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(offline)) as lt:
        queued = capture_figure_bytes(
            PNG,
            filename="nb-cell3-figure1.png",
            anchor=checkout,
            source_uri=source_uri,
            logical_id="display/nb.ipynb/cell-1/figure-1",
            client=lt,
            require_bound_project=True,
        )

    assert queued.action == "queued"
    outbox = checkout / ".lab-tracker" / "outbox" / "watch"
    event_path = Path(queued.queued_event)
    assert event_path.parent == outbox.resolve()
    blob = outbox / QUEUED_PAYLOAD_DIRNAME / f"{sha256(PNG).hexdigest()}.png"
    assert blob.read_bytes() == PNG
    written = {path for path in _files_under(checkout) if path.parts[0] != ".lab-tracker"}
    assert written == {Path("lt_ids.json")}
    event = read_event(event_path)
    assert event["source"]["uri"] == source_uri
    assert event["source"]["path"] == str(blob.resolve())
    assert event["payload"]["title"] == "nb-cell3-figure1.png"

    uploads: list[dict[str, str]] = []

    def sync_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/notes":
            return httpx.Response(
                200, json={"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        assert PNG in request.content
        uploads.append(
            {
                "client_capture_id": _multipart_field(request.content, "client_capture_id"),
                "metadata": _multipart_field(request.content, "metadata"),
            }
        )
        return httpx.Response(201, json={"data": {"note_id": "note-synced"}})

    with LabTracker(
        base_url="http://testserver", transport=httpx.MockTransport(sync_handler)
    ) as lt:
        summary = sync_outbox_path(lt, outbox)

    assert summary["errors"] == []
    assert uploads[0]["client_capture_id"] == queued.client_capture_id
    metadata = json.loads(uploads[0]["metadata"])
    assert metadata["evidence_source_uri"] == source_uri
    assert metadata["evidence_title"] == "nb-cell3-figure1.png"


def test_rerun_with_new_bytes_keeps_one_logical_capture(tmp_path: Path) -> None:
    checkout = _bound_checkout(tmp_path / "analysis")
    capture_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        capture_ids.append(_multipart_field(request.content, "client_capture_id"))
        return httpx.Response(201, json={"data": {"note_id": "note-1", "metadata": {}}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        for payload in (PNG, PNG + b"-changed"):
            capture_figure_bytes(
                payload,
                filename="fig.png",
                anchor=checkout,
                source_uri="file:///nb.ipynb#display=cell-1/figure-1",
                logical_id="display/nb.ipynb/cell-1/figure-1",
                client=lt,
            )

    assert capture_ids == ["figure:display/nb.ipynb/cell-1/figure-1"] * 2
