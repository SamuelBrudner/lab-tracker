"""What a watch sync may read and upload.

A manifest is a summary written into a watched folder, so it must not choose
the local file a sync uploads or the project that receives the note. The
bytes a sync uploads must be the bytes it checked against the scan, read from
a regular file, and a device or FIFO must never block a scan or a sync.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import lab_tracker.file_watch as file_watch
import lab_tracker_client.watch as watch_module
from lab_tracker.file_watch import (
    NotRegularFileError,
    WatchRootSymlinkError,
    file_sha256,
    fingerprint_within_root,
    open_regular_file,
    open_regular_file_within,
    stable_file_fingerprint,
)
from lab_tracker_client import LabTracker, LTValidationError
from lab_tracker_client.watch import (
    FILE_IDENTITY_SOURCE_KEYS,
    MAX_MANIFEST_BYTES,
    SINK_ACQUISITION_OUTPUT,
    SINK_STAGED_NOTE,
    WatchConfig,
    add_watch,
    event_from_manifest,
    init_config,
    load_config,
    make_event,
    read_event,
    scan_configured,
    scan_watch,
    sync_outbox,
    write_event,
)

SECRET = b"-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key\n"
SECRET_HASH = hashlib.sha256(SECRET).hexdigest()
needs_posix_special_files = pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not Path("/dev/zero").exists(),
    reason="needs POSIX FIFOs and /dev/zero",
)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WatchConfig:
    for key in ("LAB_TRACKER_WATCH_CONFIG", "LAB_TRACKER_WATCH_OUTBOX", "LAB_TRACKER_PROJECT_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    return init_config(project_id="project-1")


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    """A private key outside every watch root."""

    path = tmp_path / "home" / ".ssh" / "id_ed25519"
    path.parent.mkdir(parents=True)
    path.write_bytes(SECRET)
    return path


class _NotesServer:
    """A notes API double that records every request body."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []
        self.uploads: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(request.content)
        if request.method == "GET" and request.url.path == "/notes":
            return httpx.Response(
                200, json={"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            self.uploads.append(request.content)
            return httpx.Response(201, json={"data": {"note_id": f"note-{len(self.uploads)}"}})
        return httpx.Response(500, json={"error": {"message": "unexpected request"}})

    def client(self) -> LabTracker:
        return LabTracker(base_url="http://testserver", transport=httpx.MockTransport(self))

    def sync(self, config: WatchConfig) -> dict[str, Any]:
        with self.client() as lt:
            return sync_outbox(lt, config)

    def leaked(self) -> bool:
        return any(SECRET in body for body in self.bodies)


def _finishes(call: Callable[[], Any], seconds: float = 10.0) -> Any:
    """Run ``call`` on a daemon thread so a blocking regression fails, not hangs."""

    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread.
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "the call blocked on a special file"
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def _special_file(tmp_path: Path, kind: str) -> Path:
    if kind == "dev-zero":
        return Path("/dev/zero")
    if kind == "fifo":
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        return fifo
    directory = tmp_path / "a-directory"
    directory.mkdir()
    return directory


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")


def _write_manifest(run_dir: Path, manifest: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "lab-tracker-evidence.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _scanned_file(config: WatchConfig, tmp_path: Path) -> tuple[Path, Path]:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    evidence = inbox / "capture.md"
    evidence.write_bytes(b"bench observation\n")
    scan_watch(config, mode="files", root=inbox)
    return evidence, next(config.outbox_path().glob("*.json"))


@pytest.mark.parametrize("form", ["absolute", "dotdot", "symlink", "device"])
def test_a_manifest_cannot_point_the_sync_at_a_file(
    config: WatchConfig, secret: Path, tmp_path: Path, form: str
) -> None:
    """Even with the target's true hash and size, the manifest's path is dropped."""

    outputs = tmp_path / "outputs"
    run_dir = outputs / "run-1"
    run_dir.mkdir(parents=True)
    if form == "absolute":
        named = str(secret)
    elif form == "dotdot":
        named = "outputs/run-1/../../home/.ssh/id_ed25519"  # relative to the cwd
    elif form == "symlink":
        _symlink(run_dir / "notes.txt", secret)
        named = str(run_dir / "notes.txt")
    else:
        if not Path("/dev/zero").exists():
            pytest.skip("needs /dev/zero")
        named = "/dev/zero"
    _write_manifest(
        run_dir,
        {
            "capture_id": "run-1",
            "summary": "Decoded held-out trials.",
            "source": {
                "uri": "file:///scratch/run-1",
                "path": named,
                "root": str(secret.parent),
                "relative_path": "id_ed25519",
                "content_hash": SECRET_HASH,
                "size_bytes": len(SECRET),
                "mtime": secret.stat().st_mtime,
            },
        },
    )

    scan = scan_watch(config, mode="manifest", root=outputs)
    event = read_event(scan["imported"][0]["event_path"])
    server = _NotesServer()
    summary = _finishes(lambda: server.sync(config))

    assert not set(event["source"]) & FILE_IDENTITY_SOURCE_KEYS
    assert event["source"]["uri"] == "file:///scratch/run-1"
    assert summary["errors"] == []
    assert summary["results"][0]["action"] == "imported"
    assert len(server.uploads) == 1
    assert b"Decoded held-out trials." in server.uploads[0]
    assert not server.leaked()


def test_a_queued_manifest_event_never_uploads_the_path_it_carries(
    config: WatchConfig, secret: Path, tmp_path: Path
) -> None:
    """An event an older client queued from a manifest kept the manifest's path."""

    manifest = _write_manifest(
        tmp_path / "outputs" / "run-1", {"capture_id": "run-1", "summary": "Run summary."}
    )
    event = event_from_manifest(config, manifest)
    event["source"].update(
        {"path": str(secret), "content_hash": SECRET_HASH, "size_bytes": len(SECRET)}
    )
    write_event(event, config.outbox_path())
    server = _NotesServer()

    summary = server.sync(config)

    assert summary["errors"] == []
    assert len(server.uploads) == 1
    assert b"Run summary." in server.uploads[0]
    assert not server.leaked()


def test_a_manifest_cannot_choose_the_project(config: WatchConfig, tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path / "outputs" / "run-1",
        {
            "capture_id": "run-1",
            "project_id": "attacker-project",
            "context": {"project_id": "attacker-project", "question_id": "question-1"},
        },
    )

    configured = event_from_manifest(config, manifest)
    flagged = event_from_manifest(config, manifest, project_id="project-2")
    unbound = event_from_manifest(dataclasses.replace(config, project_id=None), manifest)

    assert configured["context"]["project_id"] == "project-1"
    # The manifest still declares its question, dataset, and session targets.
    assert configured["context"]["question_id"] == "question-1"
    assert flagged["context"]["project_id"] == "project-2"
    # Unbound, the sync falls back to the checkout binding, not the manifest.
    assert unbound["context"]["project_id"] is None


def test_a_manifest_watch_cannot_register_acquisition_outputs(
    config: WatchConfig, tmp_path: Path
) -> None:
    _write_manifest(
        tmp_path / "outputs" / "run-1",
        {
            "capture_id": "run-1",
            "source": {"relative_path": "x.bin", "content_hash": "0" * 64, "size_bytes": 1},
        },
    )

    summary = scan_watch(
        config,
        mode="manifest",
        root=tmp_path / "outputs",
        sink=SINK_ACQUISITION_OUTPUT,
        session_id="session-1",
    )

    assert summary["imported"] == []
    assert "--mode files" in summary["errors"][0]["error"]


def test_a_symlink_swapped_in_after_the_scan_is_refused(
    config: WatchConfig, tmp_path: Path
) -> None:
    """Scans record resolved paths, so a symlink there now is refused even when
    it points at identical bytes with the scanned size and mtime."""

    evidence, event_path = _scanned_file(config, tmp_path)
    twin = tmp_path / "elsewhere" / "capture.md"
    twin.parent.mkdir()
    twin.write_bytes(evidence.read_bytes())
    scanned = evidence.stat()
    os.utime(twin, ns=(scanned.st_atime_ns, scanned.st_mtime_ns))
    evidence.unlink()
    _symlink(evidence, twin)
    server = _NotesServer()

    summary = server.sync(config)

    assert summary["results"][0]["action"] == "stale"
    assert server.bodies == []
    assert read_event(event_path)["sync"]["status"] == "stale"


def test_a_swap_after_the_stale_check_cannot_change_the_upload(
    config: WatchConfig, secret: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attacker wins the race between the stale check and the upload."""

    evidence, event_path = _scanned_file(config, tmp_path)
    original_import = LabTracker.import_evidence_file

    def swap_then_import(self: LabTracker, **kwargs: Any) -> Any:
        evidence.unlink()
        _symlink(evidence, secret)
        return original_import(self, **kwargs)

    monkeypatch.setattr(LabTracker, "import_evidence_file", swap_then_import)
    server = _NotesServer()

    summary = server.sync(config)

    assert summary["errors"] == []
    assert len(server.uploads) == 1
    assert b"bench observation" in server.uploads[0]
    # The checked bytes keep their own name, not the swapped-in target's.
    assert b"id_ed25519" not in server.uploads[0]
    assert not server.leaked()
    assert read_event(event_path)["sync"]["status"] == "synced"


def test_scans_skip_symlinks_out_of_the_root(
    config: WatchConfig, secret: Path, tmp_path: Path
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "real.md").write_text("real", encoding="utf-8")
    outside_manifest = _write_manifest(
        tmp_path / "elsewhere", {"capture_id": "outside", "summary": "From outside."}
    )
    (inbox / "run-9").mkdir()
    _symlink(inbox / "key.md", secret)
    _symlink(inbox / "run-9" / "lab-tracker-evidence.json", outside_manifest)
    _symlink(inbox / "linked-dir", secret.parent)

    files = scan_watch(config, mode="files", root=inbox)
    manifests = scan_watch(config, mode="manifest", root=inbox)

    assert [Path(item["source"]).name for item in files["imported"]] == ["real.md"]
    assert manifests["matched"] == 0


@pytest.mark.parametrize("swap", ["file", "directory"])
def test_a_symlink_swapped_in_during_a_files_scan_is_not_captured(
    config: WatchConfig,
    secret: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap: str,
) -> None:
    """Discovery skips symlinks; one swapped in before the file is hashed is refused."""

    inbox = tmp_path / "inbox"
    if swap == "file":
        found, replaced, replacement = inbox / "capture.md", inbox / "capture.md", secret
    else:
        found, replaced, replacement = inbox / "sub" / "id_ed25519", inbox / "sub", secret.parent
    found.parent.mkdir(parents=True)
    found.write_bytes(b"decoy")
    original_discover = watch_module.discover_files

    def discover_then_swap(*args: Any, **kwargs: Any) -> list[Path]:
        discovered = original_discover(*args, **kwargs)
        if replaced.is_dir():
            shutil.rmtree(replaced)
        else:
            replaced.unlink()
        _symlink(replaced, replacement)
        return discovered

    monkeypatch.setattr(watch_module, "discover_files", discover_then_swap)

    summary = scan_watch(config, mode="files", root=inbox)

    assert summary["imported"] == []
    assert "inside the watch root" in summary["errors"][0]["error"]
    assert list(config.outbox_path().glob("*.json")) == []


def test_open_within_a_root_follows_no_symlink_below_it(secret: Path, tmp_path: Path) -> None:
    """The path a scan resolved is opened one directory at a time, so a
    directory swapped for a symlink after the resolve cannot lead outside."""

    root = tmp_path / "inbox"
    (root / "real").mkdir(parents=True)
    (root / "real" / "capture.md").write_bytes(b"inside")
    _symlink(root / "sub", secret.parent)
    _symlink(root / "real" / "key.md", secret)

    with open_regular_file_within(root, root / "real" / "capture.md") as handle:
        assert handle.read() == b"inside"
    for escaping in (
        root / "sub" / "id_ed25519",
        root / "real" / "key.md",
        root / ".." / "home" / ".ssh" / "id_ed25519",
        secret,
    ):
        with pytest.raises(NotRegularFileError):
            open_regular_file_within(root, escaping)
    assert stable_file_fingerprint(root / "sub" / "id_ed25519", root=root) is None


@needs_posix_special_files
@pytest.mark.parametrize("kind", ["dev-zero", "fifo", "directory"])
def test_special_files_are_refused_promptly(tmp_path: Path, kind: str) -> None:
    special = _special_file(tmp_path, kind)

    assert _finishes(lambda: stable_file_fingerprint(special)) is None
    with pytest.raises(NotRegularFileError):
        _finishes(lambda: file_sha256(special))
    with pytest.raises(NotRegularFileError):
        _finishes(lambda: open_regular_file(special, follow_symlinks=False))


@needs_posix_special_files
@pytest.mark.parametrize("sink", [SINK_STAGED_NOTE, SINK_ACQUISITION_OUTPUT])
@pytest.mark.parametrize("kind", ["dev-zero", "fifo"])
def test_sync_marks_a_special_file_stale_without_blocking(
    config: WatchConfig, tmp_path: Path, sink: str, kind: str
) -> None:
    special = _special_file(tmp_path, kind)
    event = make_event(
        capture_id="special",
        capture_kind="file",
        adapter="lt-watch-files",
        sink=sink,
        source={"path": str(special), "content_hash": "0" * 64, "size_bytes": 1},
        context={"project_id": "project-1", "session_id": "session-1"},
    )
    write_event(event, config.outbox_path())
    server = _NotesServer()

    summary = _finishes(lambda: server.sync(config))

    assert summary["results"][0]["action"] == "stale"
    assert "not a regular file" in summary["results"][0]["error"]
    assert server.bodies == []


@needs_posix_special_files
def test_import_evidence_file_refuses_a_fifo_swapped_in_before_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "bench.md"
    evidence.write_text("bench observation", encoding="utf-8")
    original_preflight = LabTracker._preflight_upload

    def preflight_then_swap(self: LabTracker, path: Path) -> int:
        size = original_preflight(self, path)
        evidence.unlink()
        os.mkfifo(evidence)
        return size

    monkeypatch.setattr(LabTracker, "_preflight_upload", preflight_then_swap)
    server = _NotesServer()

    with server.client() as lt, pytest.raises(LTValidationError, match="not a file"):
        _finishes(
            lambda: lt.import_evidence_file(
                project_id="project-1", file_path=evidence, evidence_note_index={}
            )
        )
    assert server.bodies == []


def test_import_evidence_file_uploads_supplied_bytes_without_reading_the_path(
    tmp_path: Path, secret: Path
) -> None:
    named = tmp_path / "capture.md"
    _symlink(named, secret)
    server = _NotesServer()

    with server.client() as lt:
        result = lt.import_evidence_file(
            project_id="project-1",
            file_path=named,
            payload=b"checked bytes",
            evidence_note_index={},
        )

    assert result.action == "imported"
    assert result.content_hash == hashlib.sha256(b"checked bytes").hexdigest()
    assert b"checked bytes" in server.uploads[0]
    assert b"id_ed25519" not in server.uploads[0]
    assert not server.leaked()


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_a_watch_root_replaced_by_a_symlink_is_refused(
    config: WatchConfig, secret: Path, tmp_path: Path, kind: str
) -> None:
    """Following the root would move the confinement to the link's target."""

    lab = tmp_path / "Lab"
    lab.mkdir()
    root = lab / ("results.csv" if kind == "file" else "rig1")
    _symlink(root, secret if kind == "file" else secret.parent)
    add_watch(root=str(root), config_path=config.config_path)
    configured = load_config(config_path=config.config_path)
    server = _NotesServer()

    scanned = scan_configured(configured)
    with pytest.raises(LTValidationError, match="watch root is a symlink") as refused:
        scan_watch(config, mode="files", root=root)
    summary = server.sync(configured)

    assert scanned["watches"] == []
    assert "watch root is a symlink; point the watch at its target" in str(scanned["errors"])
    assert str(secret if kind == "file" else secret.parent) in str(refused.value)
    assert list(config.outbox_path().glob("*.json")) == []
    assert summary["results"] == []
    assert not server.leaked()


def test_a_root_below_a_symlinked_folder_still_scans(config: WatchConfig, tmp_path: Path) -> None:
    """Only the root's own name must not be a link; its parents may be."""

    real = tmp_path / "real-parent"
    (real / "inbox").mkdir(parents=True)
    (real / "inbox" / "capture.md").write_bytes(b"bench observation\n")
    (real / "single.md").write_bytes(b"single file\n")
    _symlink(tmp_path / "linked-parent", real)

    folder = scan_watch(config, mode="files", root=tmp_path / "linked-parent" / "inbox")
    single = scan_watch(config, mode="files", root=tmp_path / "linked-parent" / "single.md")

    assert folder["errors"] == [] and len(folder["imported"]) == 1
    assert single["errors"] == [] and len(single["imported"]) == 1


def test_a_watch_root_swapped_for_a_symlink_during_a_scan_is_not_followed(
    config: WatchConfig, secret: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "inbox"
    root.mkdir()
    (root / "id_ed25519").write_bytes(b"decoy")
    original_discover = watch_module.discover_files

    def discover_then_swap(*args: Any, **kwargs: Any) -> list[Path]:
        discovered = original_discover(*args, **kwargs)
        shutil.rmtree(root)
        _symlink(root, secret.parent)
        return discovered

    monkeypatch.setattr(watch_module, "discover_files", discover_then_swap)

    summary = scan_watch(config, mode="files", root=root)

    assert summary["imported"] == []
    assert "watch root is a symlink" in summary["errors"][0]["error"]
    assert list(config.outbox_path().glob("*.json")) == []


def test_open_within_refuses_a_root_that_is_a_symlink(secret: Path, tmp_path: Path) -> None:
    """The root is opened ``O_NOFOLLOW`` too, so a swap after the caller's check fails."""

    root = tmp_path / "inbox"
    _symlink(root, secret.parent)

    with pytest.raises(NotRegularFileError):
        open_regular_file_within(root, root / "id_ed25519")
    with pytest.raises(WatchRootSymlinkError):
        fingerprint_within_root(root, root / "id_ed25519")
    assert stable_file_fingerprint(root / "id_ed25519", root=root) is None


def test_a_single_file_root_swapped_for_a_symlink_after_the_root_check_is_not_followed(
    secret: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single-file root is opened ``O_NOFOLLOW``, so a swap after the check is refused."""

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    root = inbox / "results.csv"
    root.write_text("a,b\n", encoding="utf-8")
    check_root = file_watch.resolve_watch_root

    def check_then_swap(path: Any) -> Path:
        checked = check_root(path)
        root.unlink()
        _symlink(root, secret)
        return checked

    monkeypatch.setattr(file_watch, "resolve_watch_root", check_then_swap)

    assert fingerprint_within_root(root, root) is None


def test_the_server_watcher_still_resolves_a_symlinked_watch_path(tmp_path: Path) -> None:
    """Only roots a client uploads from refuse a symlink; the server's
    acquisition watcher resolves its operator-configured paths as before."""

    real = tmp_path / "real"
    real.mkdir()
    (real / "frame.dat").write_bytes(b"x")
    link = tmp_path / "data"
    _symlink(link, real)

    assert file_watch.discover_files(link) == [(real / "frame.dat").resolve()]
    with pytest.raises(WatchRootSymlinkError):
        file_watch.discover_files(link, refuse_symlinked_root=True)


@pytest.mark.parametrize("swap", ["manifest", "folder"])
def test_a_manifest_swapped_for_a_symlink_after_listing_is_not_read(
    config: WatchConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap: str
) -> None:
    root = tmp_path / "watched"
    manifest = _write_manifest(root / "run-1", {"capture_id": "run-1", "summary": "benign"})
    outside = _write_manifest(
        tmp_path / "elsewhere", {"capture_id": "x", "summary": "OUTSIDE-SECRET-SUMMARY"}
    )
    original = watch_module.event_from_manifest

    def swap_then_read(*args: Any, **kwargs: Any) -> Any:
        if swap == "manifest":
            manifest.unlink()
            _symlink(manifest, outside)
        else:
            shutil.rmtree(manifest.parent)
            _symlink(manifest.parent, outside.parent)
        return original(*args, **kwargs)

    monkeypatch.setattr(watch_module, "event_from_manifest", swap_then_read)

    summary = scan_watch(config, mode="manifest", root=root)

    assert summary["imported"] == []
    assert "not a regular file inside the watch root" in summary["errors"][0]["error"]
    assert list(config.outbox_path().glob("*.json")) == []


def test_sync_does_not_follow_a_manifest_swapped_for_a_symlink(
    config: WatchConfig, tmp_path: Path
) -> None:
    """Even a link to identical bytes is refused: the scan never reads through one."""

    root = tmp_path / "watched"
    manifest = _write_manifest(root / "run-1", {"capture_id": "run-1", "summary": "Summary."})
    scan = scan_watch(config, mode="manifest", root=root)
    twin = tmp_path / "elsewhere" / "lab-tracker-evidence.json"
    twin.parent.mkdir()
    twin.write_bytes(manifest.read_bytes())
    manifest.unlink()
    _symlink(manifest, twin)
    server = _NotesServer()

    summary = server.sync(config)

    assert read_event(scan["imported"][0]["event_path"])["source"]["manifest_path"] == str(manifest)
    assert summary["results"][0]["action"] == "stale"
    assert "not a regular file" in summary["results"][0]["error"]
    assert server.bodies == []


def test_an_oversize_manifest_is_refused_before_it_is_read(
    config: WatchConfig, tmp_path: Path
) -> None:
    run_dir = tmp_path / "watched" / "run-1"
    run_dir.mkdir(parents=True)
    with (run_dir / "lab-tracker-evidence.json").open("wb") as handle:
        handle.truncate(MAX_MANIFEST_BYTES + 1)  # sparse: no disk, all zero bytes

    summary = scan_watch(config, mode="manifest", root=tmp_path / "watched")

    assert summary["imported"] == []
    assert f"over the {MAX_MANIFEST_BYTES}-byte limit" in summary["errors"][0]["error"]


def test_a_staged_note_scan_refuses_a_file_over_the_upload_limit_unhashed(
    config: WatchConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "large.bin").write_bytes(b"x" * 11)
    monkeypatch.setattr(watch_module, "MAX_UPLOAD_BYTES", 10)
    original_hash = file_watch._handle_sha256
    hashed: list[int] = []

    def counting_hash(*args: Any, **kwargs: Any) -> str:
        hashed.append(1)
        return original_hash(*args, **kwargs)

    monkeypatch.setattr(file_watch, "_handle_sha256", counting_hash)

    staged = scan_watch(config, mode="files", root=inbox)
    assert hashed == []
    # Session outputs are only registered, never uploaded: large ones are fine.
    registered = scan_watch(
        config, mode="files", root=inbox, sink=SINK_ACQUISITION_OUTPUT, session_id="session-1"
    )

    assert staged["imported"] == []
    assert "over the 10-byte upload limit" in staged["errors"][0]["error"]
    assert registered["errors"] == [] and len(registered["imported"]) == 1


def test_sync_refuses_a_hard_linked_staged_note_file(
    config: WatchConfig, secret: Path, tmp_path: Path
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    try:
        os.link(secret, inbox / "notes.md")
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")
    scan = scan_watch(config, mode="files", root=inbox)
    server = _NotesServer()

    summary = server.sync(config)

    assert len(scan["imported"]) == 1
    assert summary["results"][0]["action"] == "stale"
    assert "hard links" in summary["results"][0]["error"]
    assert server.bodies == []
    assert not server.leaked()


def test_a_hard_linked_session_output_is_still_checked_as_usual(
    config: WatchConfig, tmp_path: Path
) -> None:
    """Registering a session output sends no bytes, so hard links are allowed."""

    inbox = tmp_path / "rig"
    inbox.mkdir()
    (inbox / "frame.tif").write_bytes(b"frame")
    try:
        os.link(inbox / "frame.tif", tmp_path / "frame-copy.tif")
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")
    scan = scan_watch(
        config, mode="files", root=inbox, sink=SINK_ACQUISITION_OUTPUT, session_id="session-1"
    )

    assert watch_module._stale_reason(read_event(scan["imported"][0]["event_path"])) == ""


def test_sync_marks_a_queued_manifest_acquisition_output_event_stale(
    config: WatchConfig, secret: Path, tmp_path: Path
) -> None:
    """An older client let a manifest name a session output's path and checksum."""

    event = make_event(
        capture_id="run-1",
        capture_kind="acquisition_output",
        adapter="lt-watch-manifest",
        sink=SINK_ACQUISITION_OUTPUT,
        source={
            "manifest_path": str(tmp_path / "gone" / "lab-tracker-evidence.json"),
            "path": str(secret),
            "relative_path": "id_ed25519",
            "content_hash": SECRET_HASH,
            "size_bytes": len(SECRET),
        },
        context={"project_id": "project-1", "session_id": "session-1"},
    )
    event_path = write_event(event, config.outbox_path())
    server = _NotesServer()

    summary = server.sync(config)

    assert summary["results"][0]["action"] == "stale"
    assert "cannot register a session output" in summary["results"][0]["error"]
    assert server.bodies == []
    assert read_event(event_path)["sync"]["status"] == "stale"


needs_unprivileged_posix = pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs POSIX permissions enforced for a non-root user",
)


@needs_unprivileged_posix
def test_a_single_file_root_in_a_search_only_folder_scans(
    config: WatchConfig, tmp_path: Path
) -> None:
    """The file is opened by its own path; its folder is never opened for reading."""

    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "one.md").write_bytes(b"one\n")
    drop.chmod(0o311)
    try:
        summary = scan_watch(config, mode="files", root=drop / "one.md")
    finally:
        drop.chmod(0o755)

    assert summary["errors"] == []
    assert len(summary["imported"]) == 1


@needs_unprivileged_posix
def test_a_scan_reports_an_unreadable_file_as_a_permission_error(
    config: WatchConfig, tmp_path: Path
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    locked = inbox / "locked.md"
    locked.write_bytes(b"private\n")
    locked.chmod(0o000)
    try:
        summary = scan_watch(config, mode="files", root=inbox)
    finally:
        locked.chmod(0o644)

    assert summary["imported"] == []
    assert "permission denied reading watched file" in summary["errors"][0]["error"]


def test_the_windows_fallback_refuses_symlinked_components(
    secret: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_watch, "_WALKS_WITH_DIR_FD", False)
    root = tmp_path / "inbox"
    (root / "real").mkdir(parents=True)
    (root / "real" / "capture.md").write_bytes(b"inside")
    _symlink(root / "sub", secret.parent)
    linked_root = tmp_path / "linked-inbox"
    _symlink(linked_root, secret.parent)

    with open_regular_file_within(root, root / "real" / "capture.md") as handle:
        assert handle.read() == b"inside"
    for base, escaping in (
        (root, root / "sub" / "id_ed25519"),
        (linked_root, linked_root / "id_ed25519"),
    ):
        with pytest.raises(NotRegularFileError):
            open_regular_file_within(base, escaping)


def test_the_windows_fallback_rechecks_the_opened_path(
    secret: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder swapped for a link after the component checks, before the
    open, still fails: the opened path must resolve below the root."""

    monkeypatch.setattr(file_watch, "_WALKS_WITH_DIR_FD", False)
    root = tmp_path / "inbox"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "id_ed25519").write_bytes(b"decoy")
    original_open = file_watch.open_regular_file

    def swap_then_open(*args: Any, **kwargs: Any) -> Any:
        shutil.rmtree(root / "sub")
        _symlink(root / "sub", secret.parent)
        return original_open(*args, **kwargs)

    monkeypatch.setattr(file_watch, "open_regular_file", swap_then_open)

    with pytest.raises(NotRegularFileError):
        open_regular_file_within(root, root / "sub" / "id_ed25519")


def test_a_junction_counts_as_a_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows reports a directory junction as a reparse point, not a symlink.

    A cloud-sync placeholder (OneDrive Files On-Demand) is a reparse point too,
    but it is the file itself, so it must not be refused as a link.
    """

    junction = tmp_path / "junction"
    junction.mkdir()
    placeholder = tmp_path / "placeholder.csv"
    placeholder.write_text("x", encoding="utf-8")
    real_lstat = os.lstat

    def reparse(mode: int, tag: int) -> SimpleNamespace:
        return SimpleNamespace(
            st_mode=mode,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            st_reparse_tag=tag,
        )

    def lstat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path) == junction:
            return reparse(stat.S_IFDIR | 0o755, 0xA0000003)  # IO_REPARSE_TAG_MOUNT_POINT
        if Path(path) == placeholder:
            return reparse(stat.S_IFREG | 0o644, 0x9000001A)  # IO_REPARSE_TAG_CLOUD
        return real_lstat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", lstat)
        assert file_watch.is_link(junction)
        assert not file_watch.is_link(placeholder)
        assert not file_watch.is_link(tmp_path)
