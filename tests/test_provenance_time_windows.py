"""Time-window provenance proposals: sessions as the clock, human-gated."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from lab_tracker.api import LabTrackerAPI
from lab_tracker.app_parts.middleware import system_auth_context
from lab_tracker.models import (
    EntityOrigin,
    EntityRef,
    EntityType,
    Note,
    NoteStatus,
    ProvenanceLinkBasis,
    Session,
    SessionStatus,
    SessionType,
    encode_session_link_code,
)
from lab_tracker.photo_codes import DecodedCode, PhotoCodeDecoder
from lab_tracker.services.provenance_link_service import ProvenanceLinkService
from lab_tracker.services.provenance_time_windows import (
    TIME_WINDOW_LOOKBACK_DAYS,
    time_window_match,
)
from lab_tracker.services.session_clock import (
    FORMAT_ACQUIRED_AT_KEY,
    SESSION_HINT_METADATA_KEYS,
    capture_clock,
    parse_format_acquired_at,
    session_window,
    unique_session_at,
    zone_for_name,
)
from lab_tracker.sqlalchemy_repository_parts.repository import SQLAlchemyLabTrackerRepository

T0 = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)

# --- Unit: the capture clock and session windows -----------------------------


def _session(
    project_id: UUID,
    *,
    start: datetime,
    end: datetime | None = None,
    status: SessionStatus | None = None,
) -> Session:
    return Session(
        session_id=uuid4(),
        project_id=project_id,
        session_type=SessionType.OPERATIONAL,
        status=status or (SessionStatus.CLOSED if end is not None else SessionStatus.ACTIVE),
        started_at=start,
        ended_at=end,
        updated_at=end or start,
    )


def _note(
    project_id: UUID,
    *,
    created_at: datetime,
    metadata: dict[str, str] | None = None,
    targets: list[EntityRef] | None = None,
    status: NoteStatus = NoteStatus.STAGED,
    origin: EntityOrigin = EntityOrigin.USER,
) -> Note:
    return Note(
        note_id=uuid4(),
        project_id=project_id,
        raw_content="capture",
        metadata=dict(metadata or {}),
        targets=list(targets or []),
        status=status,
        origin=origin,
        created_at=created_at,
    )


def test_format_acquired_at_parses_z_offsets_and_reads_naive_values_as_utc() -> None:
    assert parse_format_acquired_at("2026-09-28T10:14:00Z") == T0 + timedelta(hours=1, minutes=14)
    assert parse_format_acquired_at("2026-09-28T12:14:00+02:00") == T0 + timedelta(
        hours=1, minutes=14
    )
    assert parse_format_acquired_at("2026-09-28T10:14:00") == T0 + timedelta(hours=1, minutes=14)
    for absent in (None, "", "   ", "not a date", 1_234):
        assert parse_format_acquired_at(absent) is None


def test_capture_clock_prefers_format_acquired_at_over_the_observed_time() -> None:
    project_id = uuid4()
    note = _note(
        project_id,
        created_at=T0 + timedelta(hours=5),
        metadata={
            FORMAT_ACQUIRED_AT_KEY: "2026-09-28T09:30:00Z",
            "captured_at": "2026-09-28T13:00:00+00:00",
        },
    )

    clock = capture_clock(note)

    assert (clock.at, clock.source) == (T0 + timedelta(minutes=30), "format")


def test_capture_clock_falls_back_to_the_observed_time_and_clamps_to_receipt() -> None:
    project_id = uuid4()
    unparsable = _note(
        project_id,
        created_at=T0 + timedelta(hours=5),
        metadata={FORMAT_ACQUIRED_AT_KEY: "garbage", "captured_at": "2026-09-28T10:00:00Z"},
    )
    assert (capture_clock(unparsable).at, capture_clock(unparsable).source) == (
        T0 + timedelta(hours=1),
        "client",
    )
    future = _note(
        project_id,
        created_at=T0,
        metadata={FORMAT_ACQUIRED_AT_KEY: "2026-09-29T00:00:00Z"},
    )
    assert capture_clock(future).at == T0
    plain = _note(project_id, created_at=T0)
    assert (capture_clock(plain).at, capture_clock(plain).source) == (T0, "server")


def test_session_windows_are_inclusive_and_open_sessions_run_to_now() -> None:
    project_id = uuid4()
    closed = _session(project_id, start=T0, end=T0 + timedelta(hours=1))
    open_session = _session(project_id, start=T0 + timedelta(hours=3))
    now = T0 + timedelta(hours=6)

    assert unique_session_at(T0, [closed, open_session], now=now) is closed
    assert unique_session_at(T0 + timedelta(hours=1), [closed, open_session], now=now) is closed
    assert unique_session_at(T0 + timedelta(hours=2), [closed, open_session], now=now) is None
    assert unique_session_at(now, [closed, open_session], now=now) is open_session
    assert unique_session_at(now + timedelta(seconds=1), [open_session], now=now) is None


def test_a_closed_session_without_an_end_ends_when_it_was_last_updated() -> None:
    legacy = _session(uuid4(), start=T0, status=SessionStatus.CLOSED)
    legacy.updated_at = T0 + timedelta(hours=2)

    assert session_window(legacy, now=T0 + timedelta(days=3)) == (T0, T0 + timedelta(hours=2))


def test_overlapping_session_windows_are_ambiguous_and_propose_nothing() -> None:
    project_id = uuid4()
    first = _session(project_id, start=T0, end=T0 + timedelta(hours=2))
    second = _session(project_id, start=T0 + timedelta(hours=1), end=T0 + timedelta(hours=3))
    note = _note(project_id, created_at=T0 + timedelta(hours=1, minutes=30))

    assert time_window_match(note, [first, second], now=T0 + timedelta(hours=4)) is None
    assert time_window_match(note, [first], now=T0 + timedelta(hours=4)) is not None


def test_format_acquired_at_decides_which_session_a_late_upload_belongs_to() -> None:
    """A file uploaded during the afternoon session but acquired in the
    morning belongs to the morning session."""

    project_id = uuid4()
    morning = _session(project_id, start=T0, end=T0 + timedelta(hours=2))
    afternoon = _session(project_id, start=T0 + timedelta(hours=5))
    upload = _note(
        project_id,
        created_at=T0 + timedelta(hours=6),
        metadata={FORMAT_ACQUIRED_AT_KEY: "2026-09-28T10:00:00Z"},
    )
    plain = _note(project_id, created_at=T0 + timedelta(hours=6))
    now = T0 + timedelta(hours=7)

    match = time_window_match(upload, [morning, afternoon], now=now)
    assert match is not None
    assert (match.session_id, match.clock_source) == (morning.session_id, "format")
    plain_match = time_window_match(plain, [morning, afternoon], now=now)
    assert plain_match is not None
    assert plain_match.session_id == afternoon.session_id


@pytest.mark.parametrize("key", SESSION_HINT_METADATA_KEYS)
def test_a_note_that_names_a_session_in_metadata_is_left_to_the_id_rules(key: str) -> None:
    project_id = uuid4()
    session = _session(project_id, start=T0)
    note = _note(project_id, created_at=T0 + timedelta(minutes=5), metadata={key: "anything"})

    assert time_window_match(note, [session], now=T0 + timedelta(hours=1)) is None


def test_time_window_skips_targeted_archived_reviewed_and_foreign_notes() -> None:
    project_id = uuid4()
    session = _session(project_id, start=T0)
    at = T0 + timedelta(minutes=5)
    now = T0 + timedelta(hours=1)
    other_session = EntityRef(entity_type=EntityType.SESSION, entity_id=uuid4())
    cases = {
        "session target": _note(project_id, created_at=at, targets=[other_session]),
        "archived": _note(project_id, created_at=at, status=NoteStatus.ARCHIVED),
        "review product": _note(project_id, created_at=at, origin=EntityOrigin.AI_SUGGESTED),
        "member checkpoint": _note(
            project_id,
            created_at=at,
            metadata={"member_onboarding_role": "checkpoint"},
        ),
        "another project": _note(uuid4(), created_at=at),
        "instrument booking": _note(
            project_id,
            created_at=at,
            metadata={"booking_uid": "slot-1", "booking_start": "2026-09-30T09:00:00Z"},
        ),
    }
    for label, note in cases.items():
        assert time_window_match(note, [session], now=now) is None, label
    question = EntityRef(entity_type=EntityType.QUESTION, entity_id=uuid4())
    assert (
        time_window_match(_note(project_id, created_at=at, targets=[question]), [session], now=now)
        is not None
    )


def test_zone_for_name_falls_back_to_utc() -> None:
    assert zone_for_name(None)[1] == "UTC"
    assert zone_for_name("Not/AZone")[1] == "UTC"
    assert zone_for_name("America/New_York")[1] == "America/New_York"


# --- Integration: the detector feeds the provenance-link review surface ------


class _FakeBatchClient:
    provider = "fake"
    model = "fake-batch-model"

    def draft_from_batch(self, *, batch_context: dict[str, Any], user_hint: str | None = None):
        return {
            "summary": "nothing from the model",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [],
        }

    def close(self) -> None:
        pass


def _iso(value: datetime) -> str:
    return value.isoformat()


def _project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post(
        "/projects", json={"name": f"Time window {uuid4().hex[:6]}"}, headers=headers
    )
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _session_id(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    *,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> str:
    body: dict[str, Any] = {"project_id": project_id, "session_type": "operational"}
    if started_at is not None:
        body["started_at"] = _iso(started_at)
    response = client.post("/sessions", json=body, headers=headers)
    assert response.status_code == 201, response.text
    session_id = response.json()["data"]["session_id"]
    if ended_at is not None:
        closed = client.patch(
            f"/sessions/{session_id}",
            json={"status": "closed", "ended_at": _iso(ended_at)},
            headers=headers,
        )
        assert closed.status_code == 200, closed.text
    return session_id


def _staged_note(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    text: str,
    *,
    metadata: dict[str, str] | None = None,
    targets: list[dict[str, str]] | None = None,
) -> str:
    payload: dict[str, Any] = {"project_id": project_id, "raw_content": text, "status": "staged"}
    if metadata:
        payload["metadata"] = metadata
    if targets:
        payload["targets"] = targets
    response = client.post("/notes", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["note_id"]


def _run_batch(client: TestClient, headers: dict[str, str], project_id: str) -> dict[str, Any]:
    client.app.state.graph_draft_client_factory = lambda settings: _FakeBatchClient()
    run = client.post("/batches/run-now", json={"project_id": project_id}, headers=headers)
    assert run.status_code == 201, run.text
    return run.json()["data"]


def _links(
    client: TestClient, headers: dict[str, str], project_id: str, status: str = "proposed"
) -> list[dict[str, Any]]:
    response = client.get(
        f"/provenance-links?project_id={project_id}&status={status}", headers=headers
    )
    assert response.status_code == 200
    return response.json()["data"]


def _time_window_links(
    client: TestClient, headers: dict[str, str], project_id: str, status: str = "proposed"
) -> list[dict[str, Any]]:
    return [
        link
        for link in _links(client, headers, project_id, status)
        if link["basis"] == ProvenanceLinkBasis.TIME_WINDOW_MATCH.value
    ]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_batch_run_proposes_a_time_window_link_for_a_sessionless_capture(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    start = _now() - timedelta(hours=3)
    session_id = _session_id(
        client,
        admin_auth_headers,
        project_id,
        started_at=start,
        ended_at=start + timedelta(hours=1),
    )
    inside = _staged_note(
        client,
        admin_auth_headers,
        project_id,
        "Added 5 ml buffer",
        metadata={"captured_at": _iso(start + timedelta(minutes=20))},
    )
    _staged_note(
        client,
        admin_auth_headers,
        project_id,
        "Before the session",
        metadata={"captured_at": _iso(start - timedelta(minutes=20))},
    )
    _staged_note(
        client,
        admin_auth_headers,
        project_id,
        "After the session",
    )

    run = _run_batch(client, admin_auth_headers, project_id)

    assert run["status"] == "ready"
    (link,) = _links(client, admin_auth_headers, project_id)
    assert link["source"] == {"entity_type": "note", "entity_id": inside}
    assert link["target"] == {"entity_type": "session", "entity_id": session_id}
    assert link["relation"] == "was_derived_from"
    assert link["basis"] == ProvenanceLinkBasis.TIME_WINDOW_MATCH.value
    assert link["status"] == "proposed"
    assert link["origin"] == "system_detected"
    assert link["content_hash"] is None

    accepted = client.patch(
        f"/provenance-links/{link['link_id']}",
        json={"status": "accepted"},
        headers=admin_auth_headers,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["acceptance_mode"] == "human_selected"


def test_time_window_detector_is_idempotent_and_a_decline_sticks(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    _session_id(client, admin_auth_headers, project_id)
    _staged_note(client, admin_auth_headers, project_id, "Bench photo caption")

    _run_batch(client, admin_auth_headers, project_id)
    _run_batch(client, admin_auth_headers, project_id)
    (link,) = _time_window_links(client, admin_auth_headers, project_id)

    rejected = client.patch(
        f"/provenance-links/{link['link_id']}",
        json={"status": "rejected"},
        headers=admin_auth_headers,
    )
    assert rejected.status_code == 200
    _run_batch(client, admin_auth_headers, project_id)

    assert _time_window_links(client, admin_auth_headers, project_id) == []
    assert len(_time_window_links(client, admin_auth_headers, project_id, "rejected")) == 1


def test_a_note_linked_to_any_session_is_not_given_a_second_one(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    """After a person rejects the time-window guess, a new session whose
    window also contains the capture does not get proposed in its place."""

    project_id = _project(client, admin_auth_headers)
    start = _now() - timedelta(hours=4)
    _session_id(
        client,
        admin_auth_headers,
        project_id,
        started_at=start,
        ended_at=start + timedelta(hours=2),
    )
    _staged_note(
        client,
        admin_auth_headers,
        project_id,
        "Pellet looked small",
        metadata={"captured_at": _iso(start + timedelta(minutes=30))},
    )
    _run_batch(client, admin_auth_headers, project_id)
    (link,) = _time_window_links(client, admin_auth_headers, project_id)
    client.patch(
        f"/provenance-links/{link['link_id']}",
        json={"status": "rejected"},
        headers=admin_auth_headers,
    )

    _session_id(client, admin_auth_headers, project_id, started_at=start + timedelta(minutes=10))
    _run_batch(client, admin_auth_headers, project_id)

    assert _time_window_links(client, admin_auth_headers, project_id) == []


def test_time_window_detector_stays_silent_for_overlaps_and_session_carriers(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    start = _now() - timedelta(hours=2)
    first = _session_id(client, admin_auth_headers, project_id, started_at=start)
    _session_id(client, admin_auth_headers, project_id, started_at=start + timedelta(minutes=30))
    _staged_note(
        client,
        admin_auth_headers,
        project_id,
        "Both sessions were open",
        metadata={"captured_at": _iso(start + timedelta(hours=1))},
    )
    _staged_note(
        client,
        admin_auth_headers,
        project_id,
        "Declared into its session",
        targets=[{"entity_type": "session", "entity_id": first}],
    )
    # decoded_session_link_code is server-stamped, so the carrier arrives as a
    # photo whose decoded LT- code names a session outside this project.
    label = f"LT-{encode_session_link_code(uuid4())}"
    decoder = PhotoCodeDecoder(reader=lambda _data, _type: [DecodedCode(label, "QRCode")])
    client.app.state.photo_code_decoder = decoder
    try:
        photo = client.post(
            "/notes/upload-file",
            data={"project_id": project_id},
            files={"file": ("session-qr.png", b"photo", "image/png")},
            headers=admin_auth_headers,
        )
    finally:
        decoder.close()
    assert photo.status_code == 201, photo.text
    assert photo.json()["data"]["metadata"]["decoded_session_link_code"] == label

    _run_batch(client, admin_auth_headers, project_id)

    assert _time_window_links(client, admin_auth_headers, project_id) == []


def test_time_window_detector_scans_only_recent_notes(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    _session_id(client, admin_auth_headers, project_id)
    _staged_note(client, admin_auth_headers, project_id, "Old capture")

    def propose(now: datetime) -> int:
        with client.app.state.db_session_factory() as session:
            api = LabTrackerAPI(
                repository=SQLAlchemyLabTrackerRepository(session),
                settings=client.app.state.settings,
            )
            return api.provenance_links.propose_links_from_time_windows(
                UUID(project_id), actor=system_auth_context(), now=now
            )

    later = _now() + timedelta(days=TIME_WINDOW_LOOKBACK_DAYS, hours=1)
    assert propose(later) == 0
    assert propose(_now()) == 1


def test_time_window_detector_failure_never_fails_the_batch(
    client: TestClient, admin_auth_headers: dict[str, str], monkeypatch
) -> None:
    project_id = _project(client, admin_auth_headers)
    session_id = _session_id(client, admin_auth_headers, project_id)
    _staged_note(
        client,
        admin_auth_headers,
        project_id,
        "Watched file",
        metadata={"watch_session_id": session_id},
    )

    def _explode(self, project_id, *, actor=None, now=None):
        raise RuntimeError("time-window detector exploded")

    monkeypatch.setattr(ProvenanceLinkService, "propose_links_from_time_windows", _explode)

    run = _run_batch(client, admin_auth_headers, project_id)

    assert run["status"] == "ready"
    assert [link["basis"] for link in _links(client, admin_auth_headers, project_id)] == [
        ProvenanceLinkBasis.EXACT_ID_MATCH.value
    ]


def test_session_create_accepts_a_past_start_but_not_a_future_or_naive_one(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    past = _now() - timedelta(days=1)
    created = client.post(
        "/sessions",
        json={"project_id": project_id, "session_type": "operational", "started_at": _iso(past)},
        headers=admin_auth_headers,
    )
    assert created.status_code == 201, created.text
    assert datetime.fromisoformat(created.json()["data"]["started_at"]) == past

    future = client.post(
        "/sessions",
        json={
            "project_id": project_id,
            "session_type": "operational",
            "started_at": _iso(_now() + timedelta(hours=1)),
        },
        headers=admin_auth_headers,
    )
    assert future.status_code == 422
    naive = client.post(
        "/sessions",
        json={
            "project_id": project_id,
            "session_type": "operational",
            "started_at": "2026-09-01T09:00:00",
        },
        headers=admin_auth_headers,
    )
    assert naive.status_code == 422
