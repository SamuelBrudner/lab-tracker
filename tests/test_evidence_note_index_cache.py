"""M77: outbox syncs must not re-download the project's whole note list.

The adapters dedupe uploads against an evidence-key index of existing notes.
It used to be rebuilt from a full ``/notes?project_id=`` listing on every
sync (including the post-commit hook). These tests pin the persistent,
incrementally refreshed and hit-verified cache that replaced it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

import lab_tracker_client.evidence_index as evidence_index_module
from lab_tracker_client import LabTracker
from lab_tracker_client.evidence_index import (
    CachedEvidenceNoteIndex,
    evidence_index_cache_dir,
)
from lab_tracker_client.watch import init_config, read_event, scan_watch, sync_outbox

PROJECT_ID = "11111111-1111-4111-8111-111111111111"
BASE_TIME = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class FakeNotesServer:
    """In-memory ``/notes`` list endpoint honoring project/since/until/paging."""

    def __init__(self) -> None:
        self.notes: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.uploads = 0

    def add_note(
        self,
        *,
        created_at: datetime,
        evidence: tuple[str, str, str] | None = None,
        note_id: str | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {"kind": "plain"}
        if evidence is not None:
            metadata = {
                "evidence_source_provider": evidence[0],
                "evidence_source_external_id": evidence[1],
                "evidence_content_hash": evidence[2],
            }
        note = {
            "note_id": note_id or f"note-{len(self.notes) + 1}",
            "project_id": PROJECT_ID,
            "raw_content": "x" * 64,
            "metadata": metadata,
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
        }
        self.notes.append(note)
        return note

    def list_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET" and r.url.path == "/notes"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET" and request.url.path == "/notes":
            params = request.url.params
            items = [n for n in self.notes if n["project_id"] == params.get("project_id")]
            if params.get("since"):
                since = _parse(params["since"])
                items = [n for n in items if _parse(n["created_at"]) >= since]
            if params.get("until"):
                until = _parse(params["until"])
                items = [n for n in items if _parse(n["created_at"]) < until]
            items.sort(key=lambda n: (n["created_at"], n["note_id"]))
            limit = int(params.get("limit", "50"))
            offset = int(params.get("offset", "0"))
            page = items[offset : offset + limit]
            return httpx.Response(
                200,
                json={
                    "data": page,
                    "meta": {"limit": limit, "offset": offset, "total": len(items)},
                },
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            self.uploads += 1
            metadata = {}
            for line in request.content.decode("utf-8", errors="replace").split("\r\n"):
                if line.startswith("{") and "evidence_content_hash" in line:
                    metadata = json.loads(line)
            note = self.add_note(
                created_at=BASE_TIME + timedelta(days=30, seconds=self.uploads),
                evidence=(
                    metadata["evidence_source_provider"],
                    metadata["evidence_source_external_id"],
                    metadata["evidence_content_hash"],
                ),
                note_id=f"uploaded-{self.uploads}",
            )
            return httpx.Response(201, json={"data": note})
        return httpx.Response(500, json={"error": {"message": "unexpected request"}})


def _client(server: FakeNotesServer) -> LabTracker:
    return LabTracker(base_url="http://testserver", transport=httpx.MockTransport(server.handler))


def _seed(server: FakeNotesServer, count: int) -> None:
    for index in range(count):
        server.add_note(
            created_at=BASE_TIME + timedelta(minutes=index),
            evidence=("local-folder", f"file-{index}", f"hash-{index}"),
        )


def test_second_load_refreshes_incrementally_instead_of_relisting(tmp_path: Path) -> None:
    server = FakeNotesServer()
    _seed(server, 450)
    server.add_note(created_at=BASE_TIME + timedelta(hours=10))  # non-evidence note
    cache_dir = tmp_path / "cache"

    with _client(server) as lt:
        first = lt.build_evidence_note_index(project_id=PROJECT_ID, cache_dir=cache_dir)
        full_listing = len(server.list_requests())
        server.add_note(
            created_at=BASE_TIME + timedelta(hours=11),
            evidence=("local-folder", "new-file", "new-hash"),
            note_id="note-new",
        )
        server.requests.clear()
        second = lt.build_evidence_note_index(project_id=PROJECT_ID, cache_dir=cache_dir)
        refresh_requests = server.list_requests()
        new_hit = second.get(("local-folder", "new-file", "new-hash"))

    assert isinstance(first, CachedEvidenceNoteIndex)
    assert full_listing == 3  # 451 notes in 200-record pages
    assert len(refresh_requests) == 1
    since = _parse(refresh_requests[0].url.params["since"])
    assert since == BASE_TIME + timedelta(hours=10) - evidence_index_module.REFRESH_OVERLAP
    assert new_hit is not None and new_hit["note_id"] == "note-new"
    # The fresh hit came from the incremental listing itself: no extra request.
    assert len(server.list_requests()) == 1


def test_cached_hit_is_verified_with_a_narrow_server_lookup(tmp_path: Path) -> None:
    server = FakeNotesServer()
    _seed(server, 450)
    cache_dir = tmp_path / "cache"

    with _client(server) as lt:
        lt.build_evidence_note_index(project_id=PROJECT_ID, cache_dir=cache_dir)
        server.requests.clear()
        index = lt.build_evidence_note_index(project_id=PROJECT_ID, cache_dir=cache_dir)
        server.requests.clear()
        hit = index.get(("local-folder", "file-5", "hash-5"))
        again = index.get(("local-folder", "file-5", "hash-5"))
        miss = index.get(("local-folder", "never", "seen"))

    assert hit is not None
    assert hit["note_id"] == "note-6"
    assert hit["raw_content"] == "x" * 64  # a full record, not a cache stub
    assert again is hit
    assert miss is None
    lookups = server.list_requests()
    assert len(lookups) == 1
    params = lookups[0].url.params
    created = BASE_TIME + timedelta(minutes=5)
    assert _parse(params["since"]) <= created < _parse(params["until"])
    assert _parse(params["until"]) - _parse(params["since"]) <= timedelta(seconds=2)


def test_cached_hit_for_a_deleted_note_is_evicted_and_reported_missing(tmp_path: Path) -> None:
    server = FakeNotesServer()
    _seed(server, 3)
    cache_dir = tmp_path / "cache"
    key = ("local-folder", "file-1", "hash-1")

    with _client(server) as lt:
        lt.build_evidence_note_index(project_id=PROJECT_ID, cache_dir=cache_dir)
        server.notes = [note for note in server.notes if note["note_id"] != "note-2"]
        index = lt.build_evidence_note_index(project_id=PROJECT_ID, cache_dir=cache_dir)
        assert index.get(key) is None
        server.requests.clear()
        reloaded = lt.build_evidence_note_index(project_id=PROJECT_ID, cache_dir=cache_dir)
        assert reloaded.get(key) is None

    # The eviction was persisted: the reload did not re-verify the dead entry.
    assert all("until" not in r.url.params for r in server.list_requests())


def test_cache_is_rebuilt_in_full_after_the_refresh_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeNotesServer()
    _seed(server, 3)
    cache_dir = tmp_path / "cache"
    clock = [datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(evidence_index_module, "_utc_now", lambda: clock[0])

    with _client(server) as lt:
        lt.build_evidence_note_index(project_id=PROJECT_ID, cache_dir=cache_dir)
        clock[0] += evidence_index_module.FULL_REFRESH_INTERVAL + timedelta(seconds=1)
        server.requests.clear()
        lt.build_evidence_note_index(project_id=PROJECT_ID, cache_dir=cache_dir)

    requests = server.list_requests()
    assert len(requests) == 1
    assert "since" not in requests[0].url.params


def test_cache_is_scoped_to_server_and_project(tmp_path: Path) -> None:
    server = FakeNotesServer()
    _seed(server, 3)
    cache_dir = tmp_path / "cache"

    with _client(server) as lt:
        lt.build_evidence_note_index(project_id=PROJECT_ID, cache_dir=cache_dir)
    other = FakeNotesServer()
    _seed(other, 3)
    with LabTracker(
        base_url="http://other-server", transport=httpx.MockTransport(other.handler)
    ) as lt:
        lt.build_evidence_note_index(project_id=PROJECT_ID, cache_dir=cache_dir)

    requests = other.list_requests()
    assert len(requests) == 1
    assert "since" not in requests[0].url.params
    assert len(list(cache_dir.glob("*.json"))) == 2


@pytest.mark.parametrize("corrupt", [b"{not json", b"\xff\xfe\x00garbage"])
def test_unreadable_cache_file_is_rebuilt_from_the_server(
    tmp_path: Path, corrupt: bytes
) -> None:
    server = FakeNotesServer()
    _seed(server, 3)
    cache_dir = tmp_path / "cache"

    with _client(server) as lt:
        lt.build_evidence_note_index(project_id=PROJECT_ID, cache_dir=cache_dir)
        (cache_file,) = cache_dir.glob("*.json")
        cache_file.write_bytes(corrupt)
        server.requests.clear()
        index = lt.build_evidence_note_index(project_id=PROJECT_ID, cache_dir=cache_dir)
        hit = index.get(("local-folder", "file-1", "hash-1"))

    assert hit is not None and hit["note_id"] == "note-2"
    assert "since" not in server.list_requests()[0].url.params
    assert json.loads(cache_file.read_text(encoding="utf-8"))["project_id"] == PROJECT_ID


def test_watch_resync_does_not_relist_every_project_note(tmp_path, monkeypatch) -> None:
    """Two scheduled syncs of one new capture each: the second must not page
    through the whole project note list again."""

    monkeypatch.chdir(tmp_path)
    config = init_config(project_id=PROJECT_ID)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    server = FakeNotesServer()
    _seed(server, 450)

    with _client(server) as lt:
        (inbox / "one.md").write_text("first capture", encoding="utf-8")
        scan_watch(config, mode="files", root=inbox)
        first = sync_outbox(lt, config)
        first_listing = len(server.list_requests())
        server.requests.clear()
        (inbox / "two.md").write_text("second capture", encoding="utf-8")
        scan_watch(config, mode="files", root=inbox)
        second = sync_outbox(lt, config)

    assert first["errors"] == [] and second["errors"] == []
    assert first_listing == 3
    assert len(server.list_requests()) == 1
    assert server.uploads == 2
    assert evidence_index_cache_dir(config.outbox_path()).is_dir()
    # The cache lives beside, never among, the drained ``*.json`` event files.
    events = sorted(config.outbox_path().glob("*.json"))
    assert len(events) == 2
    assert all(read_event(path)["sync"]["status"] == "synced" for path in events)


def test_dry_run_sync_does_not_write_the_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id=PROJECT_ID)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "one.md").write_text("capture", encoding="utf-8")
    scan_watch(config, mode="files", root=inbox)
    server = FakeNotesServer()

    with _client(server) as lt:
        sync_outbox(lt, config, dry_run=True)

    assert not evidence_index_cache_dir(config.outbox_path()).exists()


def test_cache_round_trip_against_the_real_notes_api(
    client, admin_auth_headers, tmp_path: Path
) -> None:
    """The ``since``/``until`` refresh and hit verification rely on the real
    list endpoint's ``created_at`` filters; exercise them end to end."""

    project = client.post("/projects", json={"name": "Cache project"}, headers=admin_auth_headers)
    assert project.status_code == 201, project.text
    project_id = project.json()["data"]["project_id"]

    def create_note(external_id: str) -> dict[str, Any]:
        response = client.post(
            "/notes",
            json={
                "project_id": project_id,
                "raw_content": f"evidence {external_id}",
                "metadata": {
                    "evidence_source_provider": "local-folder",
                    "evidence_source_external_id": external_id,
                    "evidence_content_hash": f"hash-{external_id}",
                },
            },
            headers=admin_auth_headers,
        )
        assert response.status_code == 201, response.text
        return response.json()["data"]

    def forward(request: httpx.Request) -> httpx.Response:
        response = client.request(
            request.method,
            request.url.path,
            params=request.url.params,
            content=request.content,
            headers={**admin_auth_headers, "content-type": request.headers.get("content-type", "")},
        )
        return httpx.Response(response.status_code, content=response.content)

    first_note = create_note("one")
    cache_dir = tmp_path / "cache"
    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(forward)) as lt:
        lt.build_evidence_note_index(project_id=project_id, cache_dir=cache_dir)
        second_note = create_note("two")
        index = lt.build_evidence_note_index(project_id=project_id, cache_dir=cache_dir)
        assert isinstance(index, CachedEvidenceNoteIndex)
        fresh = index.get(("local-folder", "two", "hash-two"))
        # Both notes fall inside the refresh overlap, so drop the listed records
        # to force the first key down the server-verification path.
        index._fresh.clear()
        verified = index.get(("local-folder", "one", "hash-one"))
        index._fresh.clear()
        missing = index.get(("local-folder", "one", "other-hash"))

    assert fresh is not None and fresh["note_id"] == second_note["note_id"]
    assert verified is not None and verified["note_id"] == first_note["note_id"]
    assert missing is None
