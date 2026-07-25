from __future__ import annotations

import json
from pathlib import Path

from lab_tracker_client import LTRecord
from lab_tracker_client import cli as lt_cli


def _clear_watch_env(monkeypatch) -> None:
    monkeypatch.delenv("LAB_TRACKER_WATCH_CONFIG", raising=False)
    monkeypatch.delenv("LAB_TRACKER_WATCH_OUTBOX", raising=False)


def test_watch_cli_init_scan_and_status(tmp_path, monkeypatch, capsys) -> None:
    _clear_watch_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".lab-tracker" / "watch.json"
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "capture.md").write_text("capture text", encoding="utf-8")

    lt_cli.main(
        [
            "watch",
            "init",
            "--project",
            "project-1",
            "--config",
            str(config_path),
        ]
    )
    init_payload = json.loads(capsys.readouterr().out)
    lt_cli.main(
        [
            "watch",
            "scan",
            "--config",
            str(config_path),
            "--root",
            str(inbox),
            "--include",
            "*.md",
        ]
    )
    scan_payload = json.loads(capsys.readouterr().out)
    lt_cli.main(["watch", "status", "--config", str(config_path)])
    status_payload = json.loads(capsys.readouterr().out)

    assert init_payload["command"] == "watch-init"
    assert scan_payload["command"] == "watch-scan"
    assert scan_payload["matched"] == 1
    assert status_payload["command"] == "watch-status"
    assert status_payload["pending"] == 1


def test_watch_cli_sync_acquisition_output(tmp_path, monkeypatch, capsys) -> None:
    _clear_watch_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".lab-tracker" / "watch.json"
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "output.bin").write_text("acquired", encoding="utf-8")

    lt_cli.main(["watch", "init", "--config", str(config_path)])
    capsys.readouterr()
    lt_cli.main(
        [
            "watch",
            "scan",
            "--config",
            str(config_path),
            "--root",
            str(outputs),
            "--sink",
            "acquisition-output",
            "--session",
            "session-1",
        ]
    )
    capsys.readouterr()

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def register_acquisition_output(self, session_id: str, **kwargs: object) -> LTRecord:
            self.calls.append({"session_id": session_id, **kwargs})
            return LTRecord({"output_id": "output-1"})

        def close(self) -> None:
            pass

    fake = FakeClient()
    monkeypatch.setattr(
        lt_cli.LabTracker,
        "from_env",
        classmethod(lambda cls: fake),  # noqa: ARG005
    )

    lt_cli.main(["watch", "sync", "--config", str(config_path), "--request-draft"])
    sync_payload = json.loads(capsys.readouterr().out)

    assert sync_payload["command"] == "watch-sync"
    assert sync_payload["errors"] == []
    assert sync_payload["results"][0]["output_id"] == "output-1"
    assert fake.calls[0]["session_id"] == "session-1"
    assert fake.calls[0]["file_path"] == "output.bin"
    assert isinstance(fake.calls[0]["checksum"], str)
    assert len(fake.calls[0]["checksum"]) == 64
    assert fake.calls[0]["size_bytes"] == len("acquired")


def test_watch_cli_scans_folder_as_one_complete_collection_event(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    _clear_watch_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".lab-tracker" / "watch.json"
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "trial-1.bin").write_bytes(b"one")
    (outputs / "trial-2.bin").write_bytes(b"two")

    lt_cli.main(["watch", "init", "--config", str(config_path)])
    capsys.readouterr()
    lt_cli.main(
        [
            "watch",
            "scan",
            "--config",
            str(config_path),
            "--root",
            str(outputs),
            "--sink",
            "acquisition-collection",
            "--session",
            "session-1",
            "--collection",
            "rig-2-trials",
            "--complete",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    event = json.loads(Path(payload["imported"][0]["event_path"]).read_text(encoding="utf-8"))

    assert payload["matched"] == 2
    assert len(payload["imported"]) == 1
    assert event["sink"] == "acquisition-collection"
    assert event["payload"]["collection_key"] == "rig-2-trials"
    assert event["payload"]["complete"] is True
    assert len(event["payload"]["manifest"]["members"]) == 2


def test_watch_run_reuses_collection_fingerprints_from_same_process(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    _clear_watch_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".lab-tracker" / "watch.json"
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "trial-1.bin").write_bytes(b"one")
    (outputs / "trial-2.bin").write_bytes(b"two")

    lt_cli.main(["watch", "init", "--config", str(config_path)])
    capsys.readouterr()
    lt_cli.main(
        [
            "watch",
            "add",
            str(outputs),
            "--sink",
            "acquisition-collection",
            "--session",
            "session-1",
            "--name",
            "trials",
            "--config",
            str(config_path),
        ]
    )
    capsys.readouterr()

    fingerprint_calls: list[str] = []
    original_fingerprint = lt_cli.watch_capture.stable_file_fingerprint

    def counted_fingerprint(path):
        fingerprint_calls.append(str(path))
        return original_fingerprint(path)

    monkeypatch.setattr(
        lt_cli.watch_capture,
        "stable_file_fingerprint",
        counted_fingerprint,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.snapshot_calls: list[dict[str, object]] = []

        def supports_capability(self, capability: str) -> bool:
            return capability == "acquisition_collections_v1"

        def create_acquisition_collection_snapshot(
            self,
            session_id: str,
            collection_key: str,
            **kwargs: object,
        ) -> LTRecord:
            self.snapshot_calls.append(
                {
                    "session_id": session_id,
                    "collection_key": collection_key,
                    **kwargs,
                }
            )
            return LTRecord(
                {
                    "collection_id": "collection-1",
                    "snapshot_id": "snapshot-1",
                }
            )

        def close(self) -> None:
            pass

    fake = FakeClient()
    monkeypatch.setattr(
        lt_cli.LabTracker,
        "from_env",
        classmethod(lambda cls: fake),  # noqa: ARG005
    )

    lt_cli.main(["watch", "run", "--config", str(config_path)])
    payload = json.loads(capsys.readouterr().out)

    assert payload["errors"] == []
    assert len(fingerprint_calls) == 2
    assert len(fake.snapshot_calls) == 1
    assert fake.snapshot_calls[0]["session_id"] == "session-1"
    assert fake.snapshot_calls[0]["collection_key"] == "trials"
