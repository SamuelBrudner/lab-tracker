"""Capture-setup tips on batch drafts: detection, cooldown, and the drafter's picks."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from lab_tracker.capture_setup_catalog import (
    CANDIDATES_PACKET_KEY,
    CAPTURE_SETUP_GUIDES,
    CAPTURE_SETUP_VERSION,
    EXPLANATION_MAX_CHARS,
    MAX_CANDIDATES,
    MAX_NOTE_IDS,
    MAX_RECOMMENDATIONS,
    RESPONSE_FIELD,
    RESULT_PACKET_KEY,
    THIN_CAPTURE_MAX_CHARS,
    CaptureSetupGap,
    CaptureSetupKind,
    trusted_candidates,
)
from lab_tracker.models import (
    EntityRef,
    EntityType,
    GraphChangeSet,
    GraphDraftMode,
    Note,
    NoteRawAsset,
    Session,
    SessionStatus,
    SessionType,
)
from lab_tracker.services import graph_draft_capture_setup
from lab_tracker.services.graph_draft_capture_setup import (
    detect_capture_setup_candidates,
    recently_recommended_kinds,
    record_capture_setup,
    resolve_capture_setup,
)
from lab_tracker.services.session_clock import session_label

T0 = datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=6)
PROJECT = uuid4()
OWNER = uuid4()
COLLEAGUE = uuid4()

APP = {"capture_source": "mobile_capture", "capture_mode": "text", "capture_kind": "text"}
SHORTCUT = {
    "capture_source": "mobile_capture",
    "capture_kind": "voice",
    "capture_channel": "shortcut",
}
WATCH = {"evidence_source_provider": "local-folder", "evidence_adapter": "lt-watch-files"}
LONG_TEXT = "x" * (THIN_CAPTURE_MAX_CHARS + 1)


def _session(
    start: datetime,
    end: datetime | None = None,
    *,
    author: UUID | None = OWNER,
    status: SessionStatus | None = None,
) -> Session:
    return Session(
        session_id=uuid4(),
        project_id=PROJECT,
        session_type=SessionType.OPERATIONAL,
        status=status or (SessionStatus.CLOSED if end is not None else SessionStatus.ACTIVE),
        started_at=start,
        ended_at=end,
        created_by=str(author) if author is not None else None,
        created_by_user_id=author,
    )


def _note(
    at: datetime,
    metadata: dict[str, str] | None = None,
    *,
    text: str = "pH 7.2",
    author: UUID | None = OWNER,
    session: Session | None = None,
    asset: NoteRawAsset | None = None,
) -> Note:
    targets = (
        [EntityRef(entity_type=EntityType.SESSION, entity_id=session.session_id)]
        if session is not None
        else []
    )
    return Note(
        note_id=uuid4(),
        project_id=PROJECT,
        raw_content=text,
        raw_asset=asset,
        metadata=dict(metadata or {}),
        targets=targets,
        created_at=at,
        created_by=str(author) if author is not None else None,
        created_by_user_id=author,
    )


def _photo() -> NoteRawAsset:
    return NoteRawAsset(
        storage_id=uuid4(),
        filename="IMG_1.jpg",
        content_type="image/jpeg",
        size_bytes=10,
        checksum="0" * 64,
    )


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _detect(
    notes: list[Note],
    sessions: list[Session] | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    kwargs.setdefault("owner_user_id", OWNER)
    kwargs.setdefault("now", NOW)
    return detect_capture_setup_candidates(notes, sessions or [], **kwargs)


def _by_gap(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {candidate["gap"]: candidate for candidate in candidates}


def _ids(notes: list[Note]) -> list[str]:
    return [str(note.note_id) for note in notes]


# --- Detection: phone and web captures ----------------------------------------


def test_three_own_sessionless_app_captures_are_a_detected_candidate() -> None:
    notes = [_note(_at(minute), APP) for minute in (20, 0, 10)]
    [candidate] = _detect(notes)
    assert candidate == {
        "candidate_id": "sessionless_app_captures",
        "kind": "session_capture_link",
        "gap": "sessionless_app_captures",
        "detected": True,
        "note_ids": _ids([notes[1], notes[2], notes[0]]),
        "note_count": 3,
        "session_id": None,
        "session_label": None,
    }


def test_two_sessionless_app_captures_are_offered_but_not_detected() -> None:
    [candidate] = _detect([_note(_at(0), APP), _note(_at(5), {"capture_source": "share_target"})])
    assert candidate["gap"] == "sessionless_app_captures"
    assert candidate["detected"] is False
    assert candidate["note_count"] == 2


def test_only_the_owners_captures_are_examined_or_cited() -> None:
    own = [_note(_at(minute), APP) for minute in (0, 5)]
    theirs = [_note(_at(minute), APP, author=COLLEAGUE) for minute in (1, 2, 3)]
    [candidate] = _detect([*own, *theirs])
    assert candidate["note_ids"] == _ids(own)
    assert candidate["detected"] is False
    assert _detect(theirs) == []


def test_no_user_backed_owner_gets_no_candidates() -> None:
    notes = [_note(_at(minute), APP) for minute in (0, 5, 10)]
    assert _detect(notes, owner_user_id=None) == []


def test_a_capture_inside_the_owners_own_session_is_not_sessionless() -> None:
    own_session = _session(_at(-60))
    inside = _note(_at(0), APP)
    outside = _note(_at(-120), APP)
    [candidate] = _detect([inside, outside], [own_session])
    assert candidate["note_ids"] == _ids([outside])


def test_a_capture_inside_only_a_colleagues_session_still_qualifies() -> None:
    their_session = _session(_at(-60), _at(60), author=COLLEAGUE)
    note = _note(_at(0), APP)
    [candidate] = _detect([note], [their_session])
    assert candidate["note_ids"] == _ids([note])


@pytest.mark.parametrize("channel", ["bookmarklet", "debrief"])
def test_bookmarklet_and_debrief_captures_are_not_app_captures(channel: str) -> None:
    notes = [_note(_at(minute), {**APP, "capture_channel": channel}) for minute in (0, 5, 10)]
    assert _detect(notes) == []


def test_kiosk_nfc_share_and_import_channels_are_app_captures() -> None:
    channels = ("kiosk", "nfc", "share", "import")
    notes = [
        _note(_at(index), {**APP, "capture_channel": name}) for index, name in enumerate(channels)
    ]
    [candidate] = _detect(notes)
    assert candidate["note_ids"] == _ids(notes)


def test_typed_notes_without_a_capture_source_are_not_app_captures() -> None:
    assert _detect([_note(_at(minute)) for minute in (0, 5, 10)]) == []


# --- Detection: the hands-free shortcut ---------------------------------------


def test_a_shortcut_memo_with_no_active_session_is_a_detected_gap() -> None:
    note = _note(_at(0), {**SHORTCUT, "capture_session_resolution": "none_active"})
    [candidate] = _detect([note])
    assert candidate["gap"] == "shortcut_no_active_session"
    assert candidate["kind"] == "shortcut_session"
    assert candidate["detected"] is True
    assert candidate["note_ids"] == _ids([note])


def test_a_shortcut_memo_sent_without_a_session_is_a_detected_gap() -> None:
    [candidate] = _detect([_note(_at(0), SHORTCUT)])
    assert candidate["gap"] == "shortcut_without_session"
    assert candidate["detected"] is True


@pytest.mark.parametrize("resolution", ["latest_active", "explicit"])
def test_shortcut_memos_that_reached_a_session_give_nothing(resolution: str) -> None:
    session = _session(_at(-60))
    note = _note(_at(0), {**SHORTCUT, "capture_session_resolution": resolution}, session=session)
    assert _detect([note], [session]) == []


# --- Detection: lt watch -------------------------------------------------------


def test_three_sessionless_watch_files_are_a_detected_gap() -> None:
    notes = [_note(_at(minute), WATCH, text="IMG_0001.tif") for minute in (0, 5, 10)]
    [candidate] = _detect(notes)
    assert candidate["gap"] == "sessionless_watch_files"
    assert candidate["kind"] == "watch_folder_link_code"
    assert candidate["detected"] is True
    assert _detect(notes[:2])[0]["detected"] is False


def test_a_watch_file_with_a_session_target_is_not_sessionless() -> None:
    session = _session(_at(-600), _at(-500))
    note = _note(_at(0), {**WATCH, "watch_session_source": "path"}, session=session)
    assert _detect([note], [session]) == []


def test_a_watch_file_whose_session_came_from_the_checkout_is_offered_not_detected() -> None:
    session = _session(_at(-60))
    metadata = {
        **WATCH,
        "watch_session_id": str(session.session_id),
        "watch_session_source": "active",
        "declared_target_source": "config_default",
    }
    notes = [_note(_at(minute), metadata, session=session) for minute in (0, 5, 10, 15)]
    [candidate] = _detect(notes, [session])
    assert candidate["gap"] == "watch_session_from_checkout"
    assert candidate["detected"] is False
    assert candidate["note_ids"] == _ids(notes)


# --- Detection: debriefs -------------------------------------------------------


def _closed_session_with_captures(
    texts: list[str],
    *,
    end_minutes: int = -60,
    author: UUID = OWNER,
) -> tuple[Session, list[Note]]:
    session = _session(_at(end_minutes - 60), _at(end_minutes), author=author)
    notes = [
        _note(_at(end_minutes - 50 + index), APP, text=text, author=author)
        for index, text in enumerate(texts)
    ]
    return session, notes


def test_an_own_closed_session_with_thin_captures_and_no_debrief_is_detected() -> None:
    session, notes = _closed_session_with_captures(["ok", "added buffer", "gel 2"])
    [candidate] = _detect(notes, [session])
    assert candidate == {
        "candidate_id": f"closed_without_debrief:{session.session_id}",
        "kind": "session_debrief",
        "gap": "closed_without_debrief",
        "detected": True,
        "note_ids": _ids(notes),
        "note_count": 3,
        "session_id": str(session.session_id),
        "session_label": session_label(session),
    }


def test_photos_count_as_thin_bench_captures() -> None:
    session = _session(_at(-120), _at(-60))
    notes = [_note(_at(-110 + minute), APP, text="", asset=_photo()) for minute in (0, 1, 2)]
    [candidate] = _detect(notes, [session])
    assert candidate["gap"] == "closed_without_debrief"
    assert candidate["detected"] is True


def test_a_debrief_in_the_batch_suppresses_the_sessions_candidate() -> None:
    session, notes = _closed_session_with_captures(["ok", "added buffer", "gel 2"])
    debrief = _note(
        _at(-55),
        {**APP, "capture_channel": "debrief", "capture_purpose": "session_debrief"},
        text="",
        session=session,
    )
    assert _detect([*notes, debrief], [session]) == []


def test_long_bench_captures_are_offered_but_not_detected() -> None:
    session, notes = _closed_session_with_captures(["ok", LONG_TEXT, LONG_TEXT])
    [candidate] = _detect(notes, [session])
    assert candidate["gap"] == "closed_without_debrief"
    assert candidate["detected"] is False


def test_captures_at_the_thin_limit_are_thin() -> None:
    session, notes = _closed_session_with_captures(["x" * THIN_CAPTURE_MAX_CHARS] * 3)
    [candidate] = _detect(notes, [session])
    assert candidate["detected"] is True


def test_a_colleagues_session_or_an_active_session_gives_no_debrief_candidate() -> None:
    theirs = _session(_at(-120), _at(-60), author=COLLEAGUE)
    targeted = [_note(_at(-100 + minute), APP, text="ok", session=theirs) for minute in (0, 1, 2)]
    assert _detect(targeted, [theirs]) == []
    # Even an open session with an end time recorded is not one to debrief yet.
    active = _session(_at(-120), _at(-60), status=SessionStatus.ACTIVE)
    inside = [_note(_at(-100 + minute), APP, text="ok") for minute in (0, 1, 2)]
    assert _detect(inside, [active]) == []


def test_a_session_ending_after_now_gives_no_debrief_candidate() -> None:
    session, notes = _closed_session_with_captures(["ok", "added buffer", "gel 2"])
    assert _detect(notes, [session], now=_at(-61)) == []


def test_at_most_three_debrief_sessions_newest_ended_first() -> None:
    sessions: list[Session] = []
    notes: list[Note] = []
    for end in (-400, -300, -200, -100):
        session, captures = _closed_session_with_captures(["ok"], end_minutes=end)
        sessions.append(session)
        notes.extend(captures)
    candidates = _detect(notes, sessions)
    assert [candidate["session_id"] for candidate in candidates] == [
        str(session.session_id) for session in reversed(sessions[1:])
    ]


# --- Detection: NWB headers ----------------------------------------------------


def test_nwb_files_read_without_h5py_are_a_detected_gap() -> None:
    session = _session(_at(-60))
    metadata = {**WATCH, "format_kind": "nwb", "format_sniff_error": "h5py not installed"}
    note = _note(_at(0), metadata, session=session)
    other = _note(_at(1), {**WATCH, "format_sniff_error": "truncated header"}, session=session)
    [candidate] = _detect([note, other], [session])
    assert candidate["gap"] == "nwb_headers_unread"
    assert candidate["kind"] == "nwb_h5py"
    assert candidate["detected"] is True
    assert candidate["note_ids"] == _ids([note])


# --- Detection: bounds, ordering, suppression -----------------------------------


def _one_of_every_gap() -> tuple[list[Note], list[Session]]:
    checkout = _session(_at(-1000), _at(-900))
    notes = [
        _note(_at(0), APP),
        _note(_at(1), {**SHORTCUT, "capture_session_resolution": "none_active"}),
        _note(_at(2), SHORTCUT),
        _note(_at(3), WATCH),
        _note(
            _at(4),
            {
                **WATCH,
                "watch_session_id": str(checkout.session_id),
                "watch_session_source": "active",
            },
            session=checkout,
        ),
        _note(_at(5), {**WATCH, "format_sniff_error": "h5py not installed"}, session=checkout),
    ]
    sessions = [checkout]
    for end in (-400, -300, -200):
        session, captures = _closed_session_with_captures(["ok"], end_minutes=end)
        sessions.append(session)
        notes.extend(captures)
    return notes, sessions


def test_candidates_follow_gap_order_with_debrief_sessions_last_and_are_capped() -> None:
    notes, sessions = _one_of_every_gap()
    candidates = _detect(notes, sessions)
    assert len(candidates) == MAX_CANDIDATES
    assert [candidate["gap"] for candidate in candidates] == [
        "sessionless_app_captures",
        "shortcut_no_active_session",
        "shortcut_without_session",
        "sessionless_watch_files",
        "watch_session_from_checkout",
        "nwb_headers_unread",
        "closed_without_debrief",
        "closed_without_debrief",
    ]
    # The cap drops the oldest debrief session, never a whole gap.
    assert [candidate["session_id"] for candidate in candidates[-2:]] == [
        str(sessions[3].session_id),
        str(sessions[2].session_id),
    ]


def test_suppressed_kinds_are_not_offered_and_free_their_slots() -> None:
    notes, sessions = _one_of_every_gap()
    candidates = _detect(notes, sessions, suppressed=frozenset({CaptureSetupKind.SHORTCUT_SESSION}))
    gaps = [candidate["gap"] for candidate in candidates]
    assert "shortcut_no_active_session" not in gaps
    assert "shortcut_without_session" not in gaps
    assert gaps.count("closed_without_debrief") == 3


def test_note_ids_are_capped_while_the_count_stays_whole() -> None:
    notes = [_note(_at(minute), APP) for minute in range(MAX_NOTE_IDS + 5)]
    [candidate] = _detect(notes)
    assert candidate["note_ids"] == _ids(notes[:MAX_NOTE_IDS])
    assert candidate["note_count"] == MAX_NOTE_IDS + 5


def test_detected_candidates_pass_the_trusted_whitelist_unchanged() -> None:
    notes, sessions = _one_of_every_gap()
    candidates = _detect(notes, sessions)
    assert trusted_candidates(candidates) == candidates


# --- Cooldown --------------------------------------------------------------------


def _change_set(
    packet: dict[str, Any] | None = None,
    *,
    created_at: datetime = NOW,
    mode: GraphDraftMode = GraphDraftMode.GRAPH_BATCH,
) -> GraphChangeSet:
    return GraphChangeSet(
        change_set_id=uuid4(),
        project_id=PROJECT,
        source_note_id=uuid4(),
        model="test-model",
        prompt_version="test",
        draft_mode=mode,
        context_packet=dict(packet or {}),
        created_at=created_at,
    )


def _recorded(*kinds: str) -> dict[str, Any]:
    return {RESULT_PACKET_KEY: {"recommendations": [{"kind": kind} for kind in kinds]}}


def test_a_kind_recommended_within_the_cooldown_is_suppressed() -> None:
    recent = _change_set(_recorded("shortcut_session"), created_at=NOW - timedelta(days=2))
    old = _change_set(_recorded("nwb_h5py"), created_at=NOW - timedelta(days=8))
    edge = _change_set(_recorded("session_debrief"), created_at=NOW - timedelta(days=7))
    assert recently_recommended_kinds([recent, old, edge], NOW) == frozenset(
        {CaptureSetupKind.SHORTCUT_SESSION, CaptureSetupKind.SESSION_DEBRIEF}
    )


@pytest.mark.parametrize(
    "packet",
    [
        pytest.param({RESULT_PACKET_KEY: "tips"}, id="result not a dict"),
        pytest.param({RESULT_PACKET_KEY: {"recommendations": "x"}}, id="list not a list"),
        pytest.param({RESULT_PACKET_KEY: {"recommendations": ["x", 3]}}, id="items not dicts"),
        pytest.param(_recorded("print_qr"), id="unknown kind"),
        pytest.param({}, id="no result"),
    ],
)
def test_malformed_packets_are_ignored_by_the_cooldown(packet: dict[str, Any]) -> None:
    assert recently_recommended_kinds([_change_set(packet)], NOW) == frozenset()


def test_note_scoped_drafts_do_not_count_toward_the_cooldown() -> None:
    scoped = _change_set(_recorded("nwb_h5py"), mode=GraphDraftMode.GRAPH_CONTEXT)
    assert recently_recommended_kinds([scoped], NOW) == frozenset()


# --- Resolution --------------------------------------------------------------------


def _offered() -> tuple[list[dict[str, Any]], list[Note], Session]:
    notes, sessions = _one_of_every_gap()
    app_notes = [_note(_at(10 + minute), APP) for minute in range(2)]
    candidates = _detect([*notes, *app_notes], sessions)
    return candidates, [notes[0], *app_notes], sessions[3]


def _pick(
    candidate_id: str, note_ids: list[str], explanation: str = "No session named."
) -> dict[str, Any]:
    return {"candidate_id": candidate_id, "note_ids": note_ids, "explanation": explanation}


def test_a_valid_pick_becomes_a_tip_with_the_server_guide() -> None:
    candidates, app_notes, _session = _offered()
    picked = [str(app_notes[2].note_id), str(app_notes[0].note_id)]
    result = resolve_capture_setup(
        candidates,
        [_pick("sessionless_app_captures", picked, "  These   photos\nnamed no session.")],
    )
    guide = CAPTURE_SETUP_GUIDES[CaptureSetupGap.SESSIONLESS_APP_CAPTURES]
    assert result == {
        "version": CAPTURE_SETUP_VERSION,
        "offered": [candidate["candidate_id"] for candidate in candidates],
        "returned": 1,
        "dropped": 0,
        "recommendations": [
            {
                "recommendation_id": "sessionless_app_captures",
                "kind": "session_capture_link",
                "gap": "sessionless_app_captures",
                "detected": True,
                "note_ids": [str(app_notes[0].note_id), str(app_notes[2].note_id)],
                "note_count": 2,
                "session_id": None,
                "session_label": None,
                "explanation": "These photos named no session.",
                "explanation_source": "model",
                "guide": {
                    "title": guide.title,
                    "steps": list(guide.steps),
                    "app_path": "/app",
                    "command": None,
                    "doc": guide.doc,
                },
            }
        ],
    }


def test_cited_note_ids_follow_the_candidates_capture_order() -> None:
    candidates, app_notes, _session = _offered()
    picked = list(reversed(_ids(app_notes)))
    result = resolve_capture_setup(candidates, [_pick("sessionless_app_captures", picked)])
    assert result is not None
    assert result["recommendations"][0]["note_ids"] == _ids(app_notes)


def test_the_guide_snapshot_fills_in_the_debrief_session() -> None:
    candidates, _app_notes, newest = _offered()
    debrief = next(item for item in candidates if item["session_id"] == str(newest.session_id))
    result = resolve_capture_setup(
        candidates, [_pick(debrief["candidate_id"], debrief["note_ids"])]
    )
    assert result is not None
    [tip] = result["recommendations"]
    assert tip["guide"]["app_path"] == f"/app/sessions/{newest.session_id}"
    assert tip["session_label"] == session_label(newest)


def test_foreign_note_ids_are_filtered_and_an_empty_citation_is_dropped() -> None:
    candidates, app_notes, _session = _offered()
    shortcut = next(item for item in candidates if item["gap"] == "shortcut_without_session")
    result = resolve_capture_setup(
        candidates,
        [
            _pick("sessionless_app_captures", [str(app_notes[0].note_id), str(uuid4())]),
            _pick("shortcut_without_session", [str(app_notes[1].note_id), "not-a-uuid"]),
        ],
    )
    assert result is not None
    assert result["returned"] == 2
    assert result["dropped"] == 1
    [tip] = result["recommendations"]
    assert tip["note_ids"] == [str(app_notes[0].note_id)]
    assert tip["note_count"] == 1
    assert shortcut["candidate_id"] not in {
        item["recommendation_id"] for item in result["recommendations"]
    }


def test_unknown_repeated_and_malformed_picks_are_dropped_and_counted() -> None:
    candidates, app_notes, _session = _offered()
    cited = [str(app_notes[0].note_id)]
    result = resolve_capture_setup(
        candidates,
        [
            _pick("sessionless_app_captures", cited),
            _pick("sessionless_app_captures", cited),
            _pick("print_qr", cited),
            "sessionless_app_captures",
            {"candidate_id": "shortcut_without_session"},
            {"candidate_id": "nwb_headers_unread", "note_ids": "not-a-list", "explanation": "x"},
            {"candidate_id": ["sessionless_app_captures"], "note_ids": cited, "explanation": "x"},
        ],
    )
    assert result is not None
    assert (result["returned"], result["dropped"]) == (7, 6)
    assert [tip["recommendation_id"] for tip in result["recommendations"]] == [
        "sessionless_app_captures"
    ]


@pytest.mark.parametrize(
    "explanation",
    [
        "See https://lab.example/app/devices for the shortcut.",
        "Open www.example.com first.",
        "Run `lt watch add` next time.",
        "Run lt watch add with a session.",
        "Use LT session use before saving.",
        "   ",
        None,
        "",
    ],
)
def test_unsafe_or_empty_explanations_fall_back_to_the_server_sentence(
    explanation: object,
) -> None:
    candidates, app_notes, _session = _offered()
    pick = _pick("sessionless_app_captures", _ids(app_notes))
    pick["explanation"] = explanation
    result = resolve_capture_setup(candidates, [pick])
    assert result is not None
    [tip] = result["recommendations"]
    guide = CAPTURE_SETUP_GUIDES[CaptureSetupGap.SESSIONLESS_APP_CAPTURES]
    assert tip["explanation"] == guide.server_explanation.format(count=3)
    assert tip["explanation_source"] == "server"


def test_explanations_are_cut_to_the_cap() -> None:
    candidates, app_notes, _session = _offered()
    long = "The photos named no session. " * 20
    result = resolve_capture_setup(
        candidates, [_pick("sessionless_app_captures", _ids(app_notes), long)]
    )
    assert result is not None
    [tip] = result["recommendations"]
    assert len(tip["explanation"]) <= EXPLANATION_MAX_CHARS
    assert tip["explanation"].startswith("The photos named no session. The photos")
    assert tip["explanation_source"] == "model"


def test_explanations_are_coerced_to_printable_text() -> None:
    candidates, app_notes, _session = _offered()
    pick = _pick("sessionless_app_captures", _ids(app_notes))
    pick["explanation"] = 42
    result = resolve_capture_setup(candidates, [pick])
    assert result is not None
    assert result["recommendations"][0]["explanation"] == "42"
    pick["explanation"] = "No\x00 session\x07 named."
    result = resolve_capture_setup(candidates, [pick])
    assert result is not None
    assert result["recommendations"][0]["explanation"] == "No session named."


def test_at_most_six_tips_are_kept() -> None:
    candidates, _app_notes, _session = _offered()
    picks = [_pick(item["candidate_id"], item["note_ids"]) for item in candidates]
    assert len(picks) > MAX_RECOMMENDATIONS
    result = resolve_capture_setup(candidates, picks)
    assert result is not None
    assert len(result["recommendations"]) == MAX_RECOMMENDATIONS
    assert result["returned"] == len(picks)
    assert result["dropped"] == len(picks) - MAX_RECOMMENDATIONS


@pytest.mark.parametrize("items", [None, "sessionless_app_captures", {"candidate_id": "x"}])
def test_a_missing_or_non_list_response_records_the_offer_without_tips(items: object) -> None:
    candidates, _app_notes, _session = _offered()
    assert resolve_capture_setup(candidates, items) == {
        "version": CAPTURE_SETUP_VERSION,
        "offered": [candidate["candidate_id"] for candidate in candidates],
        "returned": 0,
        "dropped": 0,
        "recommendations": [],
    }


def test_no_candidates_and_no_items_records_nothing() -> None:
    assert resolve_capture_setup(None, None) is None
    assert resolve_capture_setup([], []) is None


def test_items_without_candidates_are_recorded_as_dropped() -> None:
    result = resolve_capture_setup([], [_pick("sessionless_app_captures", [str(uuid4())])])
    assert result == {
        "version": CAPTURE_SETUP_VERSION,
        "offered": [],
        "returned": 1,
        "dropped": 1,
        "recommendations": [],
    }


def test_untrusted_candidates_in_the_packet_are_never_offered() -> None:
    candidates, app_notes, _session = _offered()
    tampered = [{**candidates[0], "kind": "print_qr"}, *candidates[1:]]
    result = resolve_capture_setup(tampered, [_pick("sessionless_app_captures", _ids(app_notes))])
    assert result is not None
    assert "sessionless_app_captures" not in result["offered"]
    assert result["recommendations"] == []
    assert result["dropped"] == 1


# --- Recording on the change set ---------------------------------------------------


def test_record_capture_setup_writes_the_result_into_the_packet() -> None:
    candidates, app_notes, _session = _offered()
    change_set = _change_set({CANDIDATES_PACKET_KEY: candidates, "other": 1})
    record_capture_setup(
        change_set, {RESPONSE_FIELD: [_pick("sessionless_app_captures", _ids(app_notes))]}
    )
    result = change_set.context_packet[RESULT_PACKET_KEY]
    assert [tip["recommendation_id"] for tip in result["recommendations"]] == [
        "sessionless_app_captures"
    ]
    assert change_set.context_packet["other"] == 1
    assert change_set.context_packet[CANDIDATES_PACKET_KEY] == candidates


def test_record_capture_setup_leaves_a_packet_without_candidates_alone() -> None:
    change_set = _change_set({"other": 1})
    record_capture_setup(change_set, {"summary": "A day."})
    assert change_set.context_packet == {"other": 1}


def test_record_capture_setup_failures_are_logged_and_change_nothing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidates, app_notes, _session = _offered()
    change_set = _change_set({CANDIDATES_PACKET_KEY: candidates})
    before = dict(change_set.context_packet)

    def explode(_candidates: object, _items: object) -> dict[str, Any] | None:
        raise RuntimeError("resolver bug")

    monkeypatch.setattr(graph_draft_capture_setup, "resolve_capture_setup", explode)
    with caplog.at_level(logging.ERROR, logger=graph_draft_capture_setup.__name__):
        record_capture_setup(
            change_set, {RESPONSE_FIELD: [_pick("sessionless_app_captures", _ids(app_notes))]}
        )
    assert change_set.context_packet == before
    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.getMessage() == (
        f"capture-setup tips failed for change set {change_set.change_set_id}"
    )
    assert record.exc_info is not None and isinstance(record.exc_info[1], RuntimeError)
