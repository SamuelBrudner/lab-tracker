"""Tests for the shared outbox drain mechanics (lab-tracker-ggzs.1).

These cover the semantics all three adapters (watch/repo/hpc) now share: a
malformed record is quarantined without aborting the batch, actionable records
are selected before the limit is spent (so a terminal-first record cannot starve
pending work), and concurrent drainers are serialized by an advisory lock.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab_tracker_client import outbox as _outbox


def _write(outbox: Path, name: str, payload: object) -> Path:
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / name
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    return path


def _read_event(path: Path) -> dict:
    data = json.loads(path.read_text())  # raises on malformed -> quarantined
    if not isinstance(data, dict):
        raise ValueError("event must be an object")
    return data


def _is_actionable(event: dict) -> bool:
    return event.get("status") != "synced"


def _make_drain(outbox: Path, processed: list[str], *, limit: int | None = None) -> dict:
    def process(path: Path, event: dict) -> dict:
        processed.append(event["id"])
        return {"action": "synced", "path": str(path), "id": event["id"]}

    def on_skipped(path: Path, event: dict) -> dict:
        return {
            "action": "skipped",
            "path": str(path),
            "id": event["id"],
            "reason": "already_synced",
        }

    def on_failure(path: Path, event: dict, exc: Exception) -> dict:
        return {"action": "failed", "path": str(path), "id": event["id"], "error": str(exc)}

    return _outbox.drain_outbox(
        outbox=outbox,
        command="test-sync",
        dry_run=False,
        request_draft=False,
        limit=limit,
        read_event=_read_event,
        is_actionable=_is_actionable,
        process=process,
        on_skipped=on_skipped,
        on_failure=on_failure,
    )


def test_malformed_record_is_quarantined_and_batch_continues(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    _write(outbox, "a.json", "{not valid json")
    _write(outbox, "b.json", {"id": "b", "status": "pending"})

    processed: list[str] = []
    summary = _make_drain(outbox, processed)

    # The valid sibling still drained.
    assert processed == ["b"]
    # The poison record was quarantined out of the *.json drain set, durably.
    assert not (outbox / "a.json").exists()
    assert (outbox / "a.json.quarantine").exists()
    error_sidecar = json.loads((outbox / "a.json.error").read_text())
    assert error_sidecar["original_path"].endswith("a.json")
    assert error_sidecar["error"]
    assert summary["quarantined"] == 1
    actions = {r["action"] for r in summary["results"]}
    assert actions == {"quarantined", "synced"}
    assert any(e["action"] == "quarantined" for e in summary["errors"])
    # The quarantined file is never re-picked; only the valid sibling remains.
    assert [p.name for p in _outbox.list_event_files(outbox)] == ["b.json"]
    assert _outbox.count_quarantined(outbox) == 1


def test_terminal_first_with_limit_one_still_reaches_pending(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    # 'a' sorts before 'b'; 'a' is already synced (terminal), 'b' is pending.
    _write(outbox, "a.json", {"id": "a", "status": "synced"})
    _write(outbox, "b.json", {"id": "b", "status": "pending"})

    processed: list[str] = []
    summary = _make_drain(outbox, processed, limit=1)

    # The terminal record does not consume the limit budget, so pending work runs.
    assert processed == ["b"]
    assert summary["processed"] == 2  # one skipped + one synced
    by_id = {r["id"]: r["action"] for r in summary["results"]}
    assert by_id == {"a": "skipped", "b": "synced"}


def test_limit_bounds_actionable_records_not_files(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    _write(outbox, "a.json", {"id": "a", "status": "pending"})
    _write(outbox, "b.json", {"id": "b", "status": "synced"})
    _write(outbox, "c.json", {"id": "c", "status": "pending"})
    _write(outbox, "d.json", {"id": "d", "status": "pending"})

    processed: list[str] = []
    summary = _make_drain(outbox, processed, limit=2)

    # 'b' (terminal) is skipped for free; the limit bounds the two actionable
    # records actually processed. 'd' is left for a later run.
    assert processed == ["a", "c"]
    assert "d" not in {r["id"] for r in summary["results"]}


def test_process_failure_is_recorded_and_batch_continues(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    _write(outbox, "a.json", {"id": "a", "status": "pending"})
    _write(outbox, "b.json", {"id": "b", "status": "pending"})

    def read_event(path: Path) -> dict:
        return _read_event(path)

    def process(path: Path, event: dict) -> dict:
        if event["id"] == "a":
            raise RuntimeError("boom")
        return {"action": "synced", "path": str(path), "id": event["id"]}

    summary = _outbox.drain_outbox(
        outbox=outbox,
        command="test-sync",
        dry_run=False,
        request_draft=False,
        limit=None,
        read_event=read_event,
        is_actionable=_is_actionable,
        process=process,
        on_skipped=lambda p, e: {"action": "skipped", "id": e["id"]},
        on_failure=lambda p, e, exc: {"action": "failed", "id": e["id"], "error": str(exc)},
    )

    by_id = {r["id"]: r["action"] for r in summary["results"]}
    assert by_id == {"a": "failed", "b": "synced"}
    assert any(e["action"] == "failed" for e in summary["errors"])


def test_advisory_lock_makes_a_second_drainer_skip(tmp_path: Path) -> None:
    fcntl = pytest.importorskip("fcntl")
    outbox = tmp_path / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    _write(outbox, "a.json", {"id": "a", "status": "pending"})

    # Hold the outbox lock the way a concurrent drainer would.
    held = open(outbox / _outbox.LOCK_FILENAME, "a+")  # noqa: SIM115 - held across drain
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        processed: list[str] = []
        summary = _make_drain(outbox, processed)
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()

    assert summary["locked"] is True
    assert processed == []
    assert summary["results"] == []

    # Once released, a fresh drain makes progress.
    processed_after: list[str] = []
    summary_after = _make_drain(outbox, processed_after)
    assert processed_after == ["a"]
    assert "locked" not in summary_after


def test_write_json_atomic_leaves_no_temp_and_uses_unique_names(tmp_path: Path) -> None:
    target = tmp_path / "event.json"
    _outbox.write_json_atomic(target, {"value": 1})
    _outbox.write_json_atomic(target, {"value": 2})
    assert json.loads(target.read_text()) == {"value": 2}
    # No leftover temp files, and the temp name is unique per write (not a fixed
    # ``event.json.tmp`` that concurrent writers would collide on).
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []
