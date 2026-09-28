"""Deterministic day-log grouping in batch drafts: one legible log per session."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from test_graph_draft_batches import FakeBatchDraftClient

from lab_tracker.models import (
    EntityRef,
    EntityType,
    GraphChangeOp,
    GraphDraftSemanticType,
    Note,
    NoteRawAsset,
    NoteStatus,
    Session,
    SessionStatus,
    SessionType,
)
from lab_tracker.services import graph_draft_day_log
from lab_tracker.services.graph_draft_day_log import (
    DAY_LOG_LINE_MAX_CHARS,
    DAY_LOG_MAX_ENTRIES,
    DAY_LOG_PACKET_KEY,
    SHORT_CAPTURE_MAX_CHARS,
    BenchCaptureKind,
    bench_capture_kind,
    day_log_body,
    day_log_operation,
    plan_day_logs,
)

T0 = datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc)
UTC = timezone.utc

# --- Unit: what counts, how it groups, what the log says ----------------------


def _session(project_id: UUID, *, start: datetime, end: datetime | None = None) -> Session:
    return Session(
        session_id=uuid4(),
        project_id=project_id,
        session_type=SessionType.OPERATIONAL,
        status=SessionStatus.CLOSED if end is not None else SessionStatus.ACTIVE,
        started_at=start,
        ended_at=end,
    )


def _asset(content_type: str, filename: str) -> NoteRawAsset:
    return NoteRawAsset(
        storage_id=uuid4(),
        filename=filename,
        content_type=content_type,
        size_bytes=10,
        checksum="0" * 64,
    )


def _note(
    project_id: UUID,
    at: datetime,
    text: str = "Added 5 ml buffer",
    *,
    asset: NoteRawAsset | None = None,
    transcript: str | None = None,
    metadata: dict[str, str] | None = None,
    targets: list[EntityRef] | None = None,
    status: NoteStatus = NoteStatus.STAGED,
) -> Note:
    return Note(
        note_id=uuid4(),
        project_id=project_id,
        raw_content=text,
        raw_asset=asset,
        transcribed_text=transcript,
        metadata=dict(metadata or {}),
        targets=list(targets or []),
        status=status,
        created_at=at,
    )


def test_bench_capture_kinds_are_short_text_transcribed_voice_and_photos() -> None:
    project_id = uuid4()
    audio = _asset("audio/webm", "memo.webm")
    cases: dict[str, tuple[Note, BenchCaptureKind | None]] = {
        "text": (_note(project_id, T0), BenchCaptureKind.TEXT),
        "long text": (_note(project_id, T0, "x" * (SHORT_CAPTURE_MAX_CHARS + 1)), None),
        "photo": (
            _note(project_id, T0, asset=_asset("image/jpeg", "IMG_1.jpg")),
            BenchCaptureKind.PHOTO,
        ),
        "voice": (
            _note(project_id, T0, asset=audio, transcript="pH reads 7.2"),
            BenchCaptureKind.VOICE,
        ),
        "untranscribed voice": (_note(project_id, T0, asset=audio), None),
        "pdf": (_note(project_id, T0, asset=_asset("application/pdf", "protocol.pdf")), None),
        "adapter import": (
            _note(project_id, T0, metadata={"evidence_source_provider": "local-folder"}),
            None,
        ),
        "booking": (_note(project_id, T0, metadata={"booking_uid": "slot"}), None),
        "earlier day log": (_note(project_id, T0, metadata={"day_log_key": "k"}), None),
        "committed": (_note(project_id, T0, status=NoteStatus.COMMITTED), None),
    }
    for label, (note, expected) in cases.items():
        assert bench_capture_kind(note) == expected, label


def test_captures_group_by_declared_named_or_timed_session() -> None:
    project_id = uuid4()
    session = _session(project_id, start=T0 - timedelta(hours=5), end=T0 - timedelta(hours=1))
    declared = _note(
        project_id,
        T0,
        "declared",
        targets=[EntityRef(entity_type=EntityType.SESSION, entity_id=session.session_id)],
    )
    named = _note(project_id, T0, "named", metadata={"watch_session_id": str(session.session_id)})
    timed = _note(
        project_id, T0, "timed", metadata={"captured_at": (T0 - timedelta(hours=4)).isoformat()}
    )
    outside = _note(project_id, T0, "outside")

    (plan,) = plan_day_logs([outside, named, declared, timed], [session], now=T0)

    assert plan.session is session
    assert {entry.note_id for entry in plan.entries} == {
        declared.note_id,
        named.note_id,
        timed.note_id,
    }
    assert plan.entries[0].note_id == timed.note_id


def test_fewer_captures_ambiguous_windows_and_recorded_logs_plan_nothing() -> None:
    project_id = uuid4()
    first = _session(project_id, start=T0 - timedelta(hours=5))
    overlapping = _session(project_id, start=T0 - timedelta(hours=4))
    notes = [_note(project_id, T0 - timedelta(hours=3, minutes=minute)) for minute in (0, 10, 20)]

    assert plan_day_logs(notes, [first, overlapping], now=T0) == []
    assert plan_day_logs(notes[:2], [first], now=T0) == []
    (plan,) = plan_day_logs(notes, [first], now=T0)
    assert plan_day_logs(notes, [first], now=T0, existing_keys={plan.key}) == []
    assert plan_day_logs(list(reversed(notes)), [first], now=T0)[0].key == plan.key


def test_day_log_body_lists_local_clock_times_and_first_lines() -> None:
    project_id = uuid4()
    session = _session(project_id, start=T0 - timedelta(hours=6))
    notes = [
        _note(project_id, T0 - timedelta(hours=5), "Added 5 ml buffer\nthen mixed"),
        _note(
            project_id,
            T0 - timedelta(hours=4),
            "voice",
            asset=_asset("audio/webm", "memo.webm"),
            transcript="  pH reads 7.2\nsecond sentence",
        ),
        _note(project_id, T0 - timedelta(hours=3), asset=_asset("image/png", "gel-lane-3.png")),
        _note(project_id, T0 - timedelta(hours=2), "y" * 300),
    ]
    (plan,) = plan_day_logs(notes, [session], now=T0)

    body = day_log_body(plan, zone=ZoneInfo("America/New_York"), zone_name="America/New_York")

    lines = body.splitlines()
    assert lines[0].startswith("Day log — operational session LT-")
    assert lines[0].endswith(", 2026-09-28 (America/New_York)")
    assert lines[2:5] == [
        "04:00 — Added 5 ml buffer",
        "05:00 — pH reads 7.2",
        "06:00 — gel-lane-3.png",
    ]
    assert lines[5].startswith("07:00 — yyy")
    assert len(lines[5]) == len("07:00 — ") + DAY_LOG_LINE_MAX_CHARS
    assert lines[5].endswith("…")


def test_day_log_body_heads_each_day_and_bounds_its_length() -> None:
    project_id = uuid4()
    session = _session(project_id, start=T0 - timedelta(days=3))
    notes = [
        _note(project_id, T0 - timedelta(days=1)),
        _note(project_id, T0 - timedelta(days=1, minutes=-5)),
        _note(project_id, T0),
    ]
    (plan,) = plan_day_logs(notes, [session], now=T0)
    body = day_log_body(plan, zone=UTC, zone_name="UTC")
    assert "2026-09-27 to 2026-09-28" in body.splitlines()[0]
    assert "2026-09-27" in body.splitlines() and "2026-09-28" in body.splitlines()

    many = [
        _note(project_id, T0 - timedelta(minutes=index), f"step {index}")
        for index in range(DAY_LOG_MAX_ENTRIES + 5)
    ]
    (big,) = plan_day_logs(many, [session], now=T0)
    big_body = day_log_body(big, zone=UTC, zone_name="UTC")
    assert big_body.count(" — step ") == DAY_LOG_MAX_ENTRIES
    assert big_body.endswith("… and 5 more captures")


def test_day_log_operation_is_a_proposed_note_citing_every_capture() -> None:
    project_id = uuid4()
    session = _session(project_id, start=T0 - timedelta(hours=6))
    notes = [_note(project_id, T0 - timedelta(hours=hours), f"step {hours}") for hours in (3, 2, 1)]
    (plan,) = plan_day_logs(notes, [session], now=T0)

    operation = day_log_operation(
        plan, change_set_id=uuid4(), sequence=4, zone=UTC, zone_name="UTC"
    )

    assert (operation.op, operation.entity_type, operation.semantic_type) == (
        GraphChangeOp.CREATE,
        EntityType.NOTE,
        GraphDraftSemanticType.CREATE_NOTE,
    )
    assert operation.sequence == 4
    assert operation.status.value == "proposed"
    assert operation.confidence is None
    assert operation.rationale.startswith("grouped 3 captures from operational session LT-")
    assert "not model output" in operation.rationale
    assert operation.payload["targets"] == [
        {"entity_type": "session", "entity_id": str(session.session_id)}
    ]
    assert operation.payload["status"] == "committed"
    assert operation.payload["metadata"]["day_log_key"] == plan.key
    assert operation.payload["metadata"]["day_log_capture_count"] == "3"
    assert [ref["source_note_ids"] for ref in operation.source_refs] == [
        [str(note.note_id)] for note in notes
    ]
    assert {ref["source_note_ids_resolution"] for ref in operation.source_refs} == {"explicit"}


# --- Integration: the batch path ---------------------------------------------


def _project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post(
        "/projects", json={"name": f"Day log {uuid4().hex[:6]}"}, headers=headers
    )
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _session_id(client: TestClient, headers: dict[str, str], project_id: str) -> str:
    response = client.post(
        "/sessions",
        json={
            "project_id": project_id,
            "session_type": "operational",
            "started_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["session_id"]


def _capture(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    text: str,
    **extra: Any,
) -> str:
    response = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": text, "status": "staged", **extra},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["note_id"]


def _model_patch(project_id: str) -> dict[str, Any]:
    return {
        "summary": "One model proposal",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [
            {
                "client_ref": "note_1",
                "op": "create",
                "entity_type": "note",
                "semantic_type": "create_note",
                "target_entity_id": None,
                "payload_json": json.dumps(
                    {"project_id": project_id, "raw_content": "Model consolidated note"}
                ),
                "rationale": "model output",
                "confidence": 0.7,
                "source_refs": [],
            }
        ],
    }


def _run_now(
    client: TestClient, headers: dict[str, str], project_id: str
) -> tuple[dict[str, Any], FakeBatchDraftClient]:
    fake = FakeBatchDraftClient(_model_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake
    response = client.post("/batches/run-now", json={"project_id": project_id}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"], fake


def _draft(client: TestClient, headers: dict[str, str], change_set_id: str) -> dict[str, Any]:
    response = client.get(f"/graph-drafts/{change_set_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _day_setup(client: TestClient, headers: dict[str, str]) -> tuple[str, str, list[str]]:
    project_id = _project(client, headers)
    session_id = _session_id(client, headers, project_id)
    captures = [
        _capture(client, headers, project_id, "Added 5 ml buffer"),
        _capture(
            client,
            headers,
            project_id,
            "Spun at 4000 g",
            targets=[{"entity_type": "session", "entity_id": session_id}],
        ),
        _capture(client, headers, project_id, "Pellet looked small"),
    ]
    return project_id, session_id, captures


def test_batch_appends_one_day_log_after_the_model_proposals(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id, session_id, captures = _day_setup(client, admin_auth_headers)

    run, fake = _run_now(client, admin_auth_headers, project_id)

    assert run["status"] == "ready"
    draft = _draft(client, admin_auth_headers, run["change_set_id"])
    model_op, day_log = draft["operations"]
    assert model_op["rationale"] == "model output"
    assert (day_log["sequence"], day_log["semantic_type"], day_log["status"]) == (
        2,
        "create_note",
        "proposed",
    )
    assert day_log["rationale"].startswith("grouped 3 captures from operational session LT-")
    assert "not model output" in day_log["rationale"]
    assert day_log["confidence"] is None
    assert day_log["payload"]["targets"] == [{"entity_type": "session", "entity_id": session_id}]
    assert [ref["source_note_ids"] for ref in day_log["source_refs"]] == [
        [note] for note in captures
    ]
    body = day_log["payload"]["raw_content"].splitlines()
    assert [line.split(" — ", 1)[1] for line in body[2:]] == [
        "Added 5 ml buffer",
        "Spun at 4000 g",
        "Pellet looked small",
    ]
    (recorded,) = draft["context_packet"][DAY_LOG_PACKET_KEY]
    assert recorded["operation_id"] == day_log["operation_id"]
    assert recorded["origin"] == "deterministic"
    assert recorded["capture_note_ids"] == captures
    # Nothing about the log was sent to the model: it is built after the call.
    (call,) = fake.calls
    assert DAY_LOG_PACKET_KEY not in call["batch_context"]


def test_rerunning_the_batch_does_not_duplicate_the_day_log(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id, _session, _captures = _day_setup(client, admin_auth_headers)
    first, _fake = _run_now(client, admin_auth_headers, project_id)

    # The same explicit window replays the same batch; the next default
    # window has no new captures and skips.
    fake = FakeBatchDraftClient(_model_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake
    replay = client.post(
        "/batches/run-now",
        json={
            "project_id": project_id,
            "since": first["window_start"],
            "until": first["window_end"],
        },
        headers=admin_auth_headers,
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["data"]["change_set_id"] == first["change_set_id"]
    later, _fake = _run_now(client, admin_auth_headers, project_id)
    assert later["change_set_id"] is None

    drafts = client.get(f"/graph-drafts?project_id={project_id}", headers=admin_auth_headers)
    assert drafts.status_code == 200
    day_logs = [
        entry
        for summary in drafts.json()["data"]
        for entry in _draft(client, admin_auth_headers, summary["change_set_id"])[
            "context_packet"
        ].get(DAY_LOG_PACKET_KEY, [])
    ]
    assert len(day_logs) == 1
    draft = _draft(client, admin_auth_headers, first["change_set_id"])
    assert [op["semantic_type"] for op in draft["operations"]] == ["create_note", "create_note"]


def test_committing_the_day_log_records_it_as_deterministic_not_the_model(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id, session_id, _captures = _day_setup(client, admin_auth_headers)
    run, _fake = _run_now(client, admin_auth_headers, project_id)
    change_set_id = run["change_set_id"]
    model_op, day_log = _draft(client, admin_auth_headers, change_set_id)["operations"]
    for operation, status in ((day_log, "accepted"), (model_op, "rejected")):
        decided = client.patch(
            f"/graph-drafts/{change_set_id}/operations/{operation['operation_id']}",
            json={"status": status},
            headers=admin_auth_headers,
        )
        assert decided.status_code == 200, decided.text

    committed = client.post(
        f"/graph-drafts/{change_set_id}/commit",
        json={"message": "keep the day log"},
        headers=admin_auth_headers,
    )

    assert committed.status_code == 200, committed.text
    applied = next(
        op
        for op in committed.json()["data"]["operations"]
        if op["operation_id"] == day_log["operation_id"]
    )
    note = client.get(f"/notes/{applied['result_entity_id']}", headers=admin_auth_headers)
    assert note.status_code == 200
    data = note.json()["data"]
    assert data["origin"] == "ai_suggested"
    assert data["origin_provider"] == "lab_tracker"
    assert data["origin_model"] == "deterministic_day_log"
    assert data["origin_prompt_version"] == "day_log/v1"
    assert data["status"] == "committed"
    assert data["targets"] == [{"entity_type": "session", "entity_id": session_id}]
    assert data["metadata"]["day_log_generator"] == "lab_tracker.day_log/v1"


def test_organize_grant_leaves_the_day_log_for_a_person(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    """create_note is outside organize: the pass must not apply the log."""

    project_id, _session, _captures = _day_setup(client, admin_auth_headers)
    granted = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings/project-default",
        json={"delegated_curation": "organize", "delegated_curation_acknowledged": True},
        headers=admin_auth_headers,
    )
    assert granted.status_code == 200, granted.text

    run, _fake = _run_now(client, admin_auth_headers, project_id)

    draft = _draft(client, admin_auth_headers, run["change_set_id"])
    assert draft["status"] == "ready"
    day_log = draft["operations"][-1]
    assert day_log["operation_id"] == draft["context_packet"][DAY_LOG_PACKET_KEY][0]["operation_id"]
    assert (day_log["status"], day_log["acceptance_mode"]) == ("proposed", None)


def test_a_failing_day_log_stage_never_fails_the_batch(
    client: TestClient, admin_auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, _session, _captures = _day_setup(client, admin_auth_headers)

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("grouping exploded")

    monkeypatch.setattr(graph_draft_day_log, "plan_day_logs", _explode)

    run, _fake = _run_now(client, admin_auth_headers, project_id)

    assert run["status"] == "ready"
    draft = _draft(client, admin_auth_headers, run["change_set_id"])
    assert [op["rationale"] for op in draft["operations"]] == ["model output"]
    assert DAY_LOG_PACKET_KEY not in draft["context_packet"]


def test_a_quiet_day_adds_no_day_log(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    _session_id(client, admin_auth_headers, project_id)
    for text in ("one", "two"):
        _capture(client, admin_auth_headers, project_id, text)

    run, _fake = _run_now(client, admin_auth_headers, project_id)

    draft = _draft(client, admin_auth_headers, run["change_set_id"])
    assert len(draft["operations"]) == 1
    assert DAY_LOG_PACKET_KEY not in draft["context_packet"]
