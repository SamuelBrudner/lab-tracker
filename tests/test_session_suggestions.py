"""Session suggestions: deterministic, computed on read, applied by a person."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from lab_tracker.models import (
    EntityOrigin,
    EntityRef,
    EntityType,
    Note,
    NoteStatus,
    Session,
    SessionStatus,
    SessionType,
)
from lab_tracker.services.session_suggestions import (
    MIN_CAPTURES_FOR_SESSION,
    QUIET_SESSION_THRESHOLD,
    SessionSuggestionKind,
    suggest_sessions,
)

T0 = datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc)
UTC = timezone.utc

# --- Unit: the pure suggestion rules ------------------------------------------


def _session(
    project_id: UUID,
    *,
    start: datetime,
    end: datetime | None = None,
) -> Session:
    return Session(
        session_id=uuid4(),
        project_id=project_id,
        session_type=SessionType.OPERATIONAL,
        status=SessionStatus.CLOSED if end is not None else SessionStatus.ACTIVE,
        started_at=start,
        ended_at=end,
    )


def _note(
    project_id: UUID,
    at: datetime,
    *,
    metadata: dict[str, str] | None = None,
    targets: list[EntityRef] | None = None,
    status: NoteStatus = NoteStatus.STAGED,
    origin: EntityOrigin = EntityOrigin.USER,
) -> Note:
    return Note(
        note_id=uuid4(),
        project_id=project_id,
        raw_content="bench capture",
        metadata=dict(metadata or {}),
        targets=list(targets or []),
        status=status,
        origin=origin,
        created_at=at,
    )


def _suggest(
    project_id: UUID,
    *,
    sessions: list[Session] | None = None,
    captures_by_session: dict[UUID, list[Note]] | None = None,
    candidates: list[Note] | None = None,
    bookings: list[Note] | None = None,
    now: datetime = T0,
    zone=UTC,
):
    return suggest_sessions(
        project_id=project_id,
        sessions=sessions or [],
        captures_by_session=captures_by_session or {},
        candidate_notes=candidates or [],
        booking_notes=bookings or [],
        now=now,
        zone=zone,
    )


def test_a_quiet_active_session_is_suggested_to_end_at_its_last_capture() -> None:
    project_id = uuid4()
    session = _session(project_id, start=T0 - timedelta(hours=8))
    early = _note(project_id, T0 - timedelta(hours=7))
    last = _note(project_id, T0 - QUIET_SESSION_THRESHOLD - timedelta(minutes=1))

    (suggestion,) = _suggest(
        project_id, sessions=[session], captures_by_session={session.session_id: [last, early]}
    )

    assert suggestion.kind == SessionSuggestionKind.CLOSE_QUIET_SESSION
    assert suggestion.session_id == session.session_id
    assert suggestion.end_at == last.created_at
    assert suggestion.capture_note_ids == [last.note_id]
    assert suggestion.suggestion_id == (f"close_quiet_session:{session.session_id}:{last.note_id}")


def test_close_suggestions_skip_busy_closed_and_empty_sessions() -> None:
    project_id = uuid4()
    busy = _session(project_id, start=T0 - timedelta(hours=8))
    closed = _session(project_id, start=T0 - timedelta(hours=8), end=T0 - timedelta(hours=6))
    empty = _session(project_id, start=T0 - timedelta(days=2))
    recent = _note(project_id, T0 - QUIET_SESSION_THRESHOLD + timedelta(minutes=1))
    old = _note(project_id, T0 - timedelta(hours=7))

    assert (
        _suggest(
            project_id,
            sessions=[busy, closed, empty],
            captures_by_session={busy.session_id: [old, recent], closed.session_id: [old]},
        )
        == []
    )


def test_a_quiet_session_uses_format_acquired_at_as_the_capture_time() -> None:
    project_id = uuid4()
    session = _session(project_id, start=T0 - timedelta(hours=8))
    upload = _note(
        project_id,
        T0 - timedelta(minutes=5),
        metadata={"format_acquired_at": (T0 - timedelta(hours=6)).isoformat()},
    )

    (suggestion,) = _suggest(
        project_id, sessions=[session], captures_by_session={session.session_id: [upload]}
    )

    assert suggestion.end_at == T0 - timedelta(hours=6)


def test_a_day_of_sessionless_captures_suggests_a_session_spanning_them() -> None:
    project_id = uuid4()
    times = [T0 - timedelta(hours=4), T0 - timedelta(hours=3), T0 - timedelta(hours=1)]
    notes = [_note(project_id, at) for at in reversed(times)]

    (suggestion,) = _suggest(project_id, candidates=notes)

    assert suggestion.kind == SessionSuggestionKind.START_SESSION_FROM_CAPTURES
    assert (suggestion.start_at, suggestion.end_at) == (times[0], times[-1])
    assert suggestion.local_date == date(2026, 9, 28)
    assert suggestion.capture_count == MIN_CAPTURES_FOR_SESSION
    assert suggestion.capture_note_ids == [note.note_id for note in reversed(notes)]
    assert suggestion.suggestion_id == f"start_session_from_captures:{project_id}:2026-09-28"


def test_capture_days_ignore_sessioned_windowed_reviewed_and_few_captures() -> None:
    project_id = uuid4()
    session = _session(project_id, start=T0 - timedelta(hours=5), end=T0 - timedelta(hours=4))
    carried = EntityRef(entity_type=EntityType.SESSION, entity_id=uuid4())
    notes = [
        _note(project_id, T0 - timedelta(hours=4, minutes=30)),  # inside the session
        _note(project_id, T0 - timedelta(hours=3), targets=[carried]),
        _note(project_id, T0 - timedelta(hours=3), metadata={"photo_session_id": "x"}),
        _note(project_id, T0 - timedelta(hours=2), status=NoteStatus.COMMITTED),
        _note(project_id, T0 - timedelta(hours=2), origin=EntityOrigin.AI_SUGGESTED),
        _note(project_id, T0 - timedelta(hours=1)),
        _note(project_id, T0 - timedelta(minutes=30)),
    ]

    assert _suggest(project_id, sessions=[session], candidates=notes) == []


def test_capture_days_are_local_days_in_the_projects_zone() -> None:
    """23:30 and 00:30 UTC are one evening in New York, two days in UTC."""

    project_id = uuid4()
    late = datetime(2026, 9, 28, 23, 30, tzinfo=UTC)
    notes = [
        _note(project_id, late - timedelta(minutes=30)),
        _note(project_id, late),
        _note(project_id, late + timedelta(hours=1)),
    ]
    now = late + timedelta(hours=2)

    assert _suggest(project_id, candidates=notes, now=now) == []
    (suggestion,) = _suggest(
        project_id, candidates=notes, now=now, zone=ZoneInfo("America/New_York")
    )
    assert suggestion.local_date == date(2026, 9, 28)
    assert "19:00-20:30" in suggestion.title


def _booking(project_id: UUID, start: datetime, end: datetime, **extra: str) -> Note:
    metadata = {
        "booking_uid": extra.pop("uid", "booking-1@calendar"),
        "booking_start": start.isoformat(),
        "booking_end": end.isoformat(),
        "booking_instrument": "Confocal 2",
        "booking_summary": "Imaging slot",
        **extra,
    }
    return _note(project_id, start - timedelta(days=1), metadata=metadata)


def test_an_uncovered_booking_suggests_a_session_for_its_window() -> None:
    project_id = uuid4()
    start, end = T0 - timedelta(hours=3), T0 - timedelta(hours=1)
    booking = _booking(project_id, start, end)
    inside = [_note(project_id, start + timedelta(minutes=minute)) for minute in (10, 20, 30)]

    (suggestion,) = _suggest(project_id, candidates=[booking, *inside], bookings=[booking])

    assert suggestion.kind == SessionSuggestionKind.START_SESSION_FROM_BOOKING
    assert (suggestion.start_at, suggestion.end_at) == (start, end)
    assert suggestion.booking_note_id == booking.note_id
    assert suggestion.booking_instrument == "Confocal 2"
    assert suggestion.capture_note_ids == [booking.note_id, *(note.note_id for note in inside)]
    assert suggestion.suggestion_id.startswith(f"start_session_from_booking:{project_id}:")
    # The captures inside the booking are the booking suggestion's, not a day's.
    assert all(
        item.kind != SessionSuggestionKind.START_SESSION_FROM_CAPTURES
        for item in _suggest(project_id, candidates=[booking, *inside], bookings=[booking])
    )


def test_bookings_that_are_covered_upcoming_or_duplicated_suggest_at_most_once() -> None:
    project_id = uuid4()
    start, end = T0 - timedelta(hours=3), T0 - timedelta(hours=1)
    first = _booking(project_id, start, end)
    resynced = _booking(project_id, start, end)
    resynced.created_at = first.created_at + timedelta(minutes=5)
    upcoming = _booking(project_id, T0 + timedelta(hours=1), T0 + timedelta(hours=2), uid="u2")
    overlapping = _session(
        project_id, start=end - timedelta(minutes=10), end=end + timedelta(hours=1)
    )

    (suggestion,) = _suggest(project_id, bookings=[first, resynced, upcoming])
    assert suggestion.booking_note_id == resynced.note_id
    assert _suggest(project_id, sessions=[overlapping], bookings=[first, upcoming]) == []


def test_an_ongoing_booking_is_suggested_with_its_future_end() -> None:
    project_id = uuid4()
    booking = _booking(project_id, T0 - timedelta(minutes=30), T0 + timedelta(hours=1))

    (suggestion,) = _suggest(project_id, bookings=[booking])

    assert suggestion.end_at == T0 + timedelta(hours=1)


def test_suggestion_ids_are_stable_across_reads() -> None:
    project_id = uuid4()
    session = _session(project_id, start=T0 - timedelta(hours=8))
    last = _note(project_id, T0 - timedelta(hours=6))
    # Yesterday's captures (the open session only began today) and an older booking.
    notes = [_note(project_id, T0 - timedelta(days=1, hours=hours)) for hours in (1, 2, 3)]
    booking = _booking(project_id, T0 - timedelta(days=2, hours=3), T0 - timedelta(days=2))

    def ids(now: datetime) -> list[str]:
        return [
            item.suggestion_id
            for item in _suggest(
                project_id,
                sessions=[session],
                captures_by_session={session.session_id: [last]},
                candidates=notes,
                bookings=[booking],
                now=now,
            )
        ]

    assert ids(T0) == ids(T0 + timedelta(minutes=10))
    assert len(ids(T0)) == 3


# --- Integration: the route ---------------------------------------------------


def _iso(value: datetime) -> str:
    return value.isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _yesterday_at(hour: int, minute: int = 0) -> datetime:
    """A fixed clock time yesterday (UTC), so a day's captures never straddle midnight."""

    return (_now() - timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)


def _project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post(
        "/projects", json={"name": f"Suggestions {uuid4().hex[:6]}"}, headers=headers
    )
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _note_id(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    *,
    captured_at: datetime,
    targets: list[dict[str, str]] | None = None,
    metadata: dict[str, str] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "project_id": project_id,
        "raw_content": "bench capture",
        "status": "staged",
        "metadata": {"captured_at": _iso(captured_at), **(metadata or {})},
    }
    if targets:
        payload["targets"] = targets
    response = client.post("/notes", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["note_id"]


def _suggestions(client: TestClient, headers: dict[str, str], project_id: str) -> dict[str, Any]:
    response = client.get(f"/projects/{project_id}/session-suggestions", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_session_suggestions_route_reports_each_kind_and_its_settings(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    now = _now()
    started = client.post(
        "/sessions",
        json={
            "project_id": project_id,
            "session_type": "operational",
            "started_at": _iso(now - timedelta(hours=9)),
        },
        headers=admin_auth_headers,
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["data"]["session_id"]
    last = _note_id(
        client,
        admin_auth_headers,
        project_id,
        captured_at=now - timedelta(hours=8),
        targets=[{"entity_type": "session", "entity_id": session_id}],
    )
    day_notes = [
        _note_id(client, admin_auth_headers, project_id, captured_at=_yesterday_at(10, minute))
        for minute in (0, 20, 40)
    ]
    booking_start = now - timedelta(days=2, hours=2)
    booking = _note_id(
        client,
        admin_auth_headers,
        project_id,
        captured_at=booking_start - timedelta(days=1),
        metadata={
            "booking_uid": "slot-42",
            "booking_start": _iso(booking_start),
            "booking_end": _iso(booking_start + timedelta(hours=1)),
            "booking_instrument": "Cytometer",
        },
    )

    report = _suggestions(client, admin_auth_headers, project_id)

    assert report["project_id"] == project_id
    assert report["timezone"] == "UTC"
    assert report["quiet_threshold_minutes"] == 240
    assert report["min_captures_per_day"] == MIN_CAPTURES_FOR_SESSION
    by_kind = {item["kind"]: item for item in report["suggestions"]}
    assert set(by_kind) == {kind.value for kind in SessionSuggestionKind}
    close = by_kind["close_quiet_session"]
    assert close["session_id"] == session_id
    assert close["capture_note_ids"] == [last]
    day = by_kind["start_session_from_captures"]
    assert sorted(day["capture_note_ids"]) == sorted(day_notes)
    booked = by_kind["start_session_from_booking"]
    assert booked["booking_note_id"] == booking
    assert booked["booking_uid"] == "slot-42"
    assert booked["capture_note_ids"] == [booking]
    # Reading is side-effect free and stable.
    assert (
        _suggestions(client, admin_auth_headers, project_id)["suggestions"] == report["suggestions"]
    )


def test_applying_a_capture_day_suggestion_through_the_session_apis_clears_it(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    for minute in (0, 20, 45):
        _note_id(client, admin_auth_headers, project_id, captured_at=_yesterday_at(10, minute))
    (suggestion,) = _suggestions(client, admin_auth_headers, project_id)["suggestions"]
    assert suggestion["kind"] == "start_session_from_captures"

    created = client.post(
        "/sessions",
        json={
            "project_id": project_id,
            "session_type": "operational",
            "started_at": suggestion["start_at"],
        },
        headers=admin_auth_headers,
    )
    assert created.status_code == 201, created.text
    closed = client.patch(
        f"/sessions/{created.json()['data']['session_id']}",
        json={"status": "closed", "ended_at": suggestion["end_at"]},
        headers=admin_auth_headers,
    )
    assert closed.status_code == 200, closed.text

    assert _suggestions(client, admin_auth_headers, project_id)["suggestions"] == []


def test_session_suggestions_need_project_read_access(
    client: TestClient, admin_auth_headers: dict[str, str], scoped_project_member
) -> None:
    visible = client.get(
        f"/projects/{scoped_project_member.visible_project_id}/session-suggestions",
        headers=scoped_project_member.member_headers,
    )
    assert visible.status_code == 200, visible.text
    assert visible.json()["data"]["suggestions"] == []

    hidden = client.get(
        f"/projects/{scoped_project_member.hidden_project_id}/session-suggestions",
        headers=scoped_project_member.member_headers,
    )
    assert hidden.status_code == 404
    missing = client.get(
        f"/projects/{uuid4()}/session-suggestions", headers=scoped_project_member.member_headers
    )
    assert missing.status_code == 404
    anonymous = client.get(
        f"/projects/{scoped_project_member.visible_project_id}/session-suggestions"
    )
    assert anonymous.status_code == 401


def test_a_viewer_can_read_suggestions_but_not_apply_them(
    client: TestClient, admin_auth_headers: dict[str, str], scoped_project_member
) -> None:
    project_id = scoped_project_member.visible_project_id
    for minute in (0, 20, 45):
        _note_id(client, admin_auth_headers, project_id, captured_at=_yesterday_at(10, minute))
    (suggestion,) = _suggestions(client, scoped_project_member.member_headers, project_id)[
        "suggestions"
    ]

    applied = client.post(
        "/sessions",
        json={
            "project_id": project_id,
            "session_type": "operational",
            "started_at": suggestion["start_at"],
        },
        headers=scoped_project_member.member_headers,
    )
    assert applied.status_code == 403
