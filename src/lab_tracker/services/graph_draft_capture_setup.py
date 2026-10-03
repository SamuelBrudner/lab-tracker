"""Capture-setup tips for batch drafts: find the gaps, keep the drafter's picks.

The batch drafter often cannot place or interpret a capture because of how it
was made: a phone photo that named no session, a shortcut memo sent while no
session was open, a watched file with no session, a session closed without a
debrief, an NWB file synced without ``h5py``. This module is the pure,
deterministic half of capture-setup tips (the copy lives in
:mod:`lab_tracker.capture_setup_catalog`):

* :func:`detect_capture_setup_candidates` derives candidates from metadata of
  the reviewer's own staged batch captures and the project's sessions. Time
  ties a capture to a session only when the same person ran it (see
  :func:`~lab_tracker.services.session_clock.sessions_for_captures`).
  Candidates carry ids, enum values, counts, and a session's own label --
  never capture text.
* :func:`recently_recommended_kinds` is the cooldown: a kind recommended on
  one of the reviewer's batch drafts within
  :data:`~lab_tracker.capture_setup_catalog.COOLDOWN_DAYS` is not offered.
* :func:`resolve_capture_setup` keeps only the drafter's picks that name an
  offered candidate and cite that candidate's own notes, cleans each
  explanation, caps the list, and attaches the server's guide. There is no
  fallback: no usable pick means no tip, and the packet still records what was
  offered, returned, and dropped.
* :func:`record_capture_setup` writes the result into the change set's
  ``context_packet``. It is best effort: a failure is logged and the change
  set is left as it was, so a tip can never fail a batch.

Tips are not operations: nothing here can be accepted, committed, delegated,
or counted as a clarification.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final
from uuid import UUID

from lab_tracker.capture_client_release import EVIDENCE_ADAPTER_KEY, WATCH_ADAPTER_PREFIX
from lab_tracker.capture_setup_catalog import (
    CANDIDATES_PACKET_KEY,
    CAPTURE_SETUP_GUIDES,
    CAPTURE_SETUP_VERSION,
    COOLDOWN_DAYS,
    EXPLANATION_MAX_CHARS,
    MAX_CANDIDATES,
    MAX_DEBRIEF_SESSIONS,
    MAX_NOTE_IDS,
    MAX_RECOMMENDATIONS,
    RESPONSE_FIELD,
    RESULT_PACKET_KEY,
    SESSION_ID_PLACEHOLDER,
    THIN_CAPTURE_MAX_CHARS,
    CaptureSetupGap,
    CaptureSetupKind,
    candidate_id_for,
    trusted_candidates,
)
from lab_tracker.models import (
    GraphChangeSet,
    GraphDraftMode,
    Note,
    NoteStatus,
    Session,
    SessionStatus,
)
from lab_tracker.services.graph_draft_batch_policy import as_utc
from lab_tracker.services.graph_draft_day_log import bench_capture_kind
from lab_tracker.services.session_clock import (
    SessionTimeline,
    capture_clock,
    eligible_sessions,
    session_label,
    session_targets,
    sessions_for_captures,
)
from lab_tracker.services.session_suggestions import (
    MIN_CAPTURES_FOR_SESSION,
    is_sessionless_capture,
)

logger = logging.getLogger(__name__)

# Phone, web capture page, share, kiosk, NFC and import captures
# (shared/capture-upload.js, shared/share-target-inbox.js).
CAPTURE_SOURCE_KEY: Final = "capture_source"
APP_CAPTURE_SOURCES: Final = frozenset({"mobile_capture", "share_target"})
# How a bench capture was made (bench-helpers.js CAPTURE_CHANNEL); these
# channels have their own gap or carry no session by design.
CAPTURE_CHANNEL_KEY: Final = "capture_channel"
SHORTCUT_CHANNEL: Final = "shortcut"
NON_APP_CAPTURE_CHANNELS: Final = frozenset({SHORTCUT_CHANNEL, "bookmarklet", "debrief"})
# routes/voice_capture.py: how a shortcut memo's session was chosen.
CAPTURE_SESSION_RESOLUTION_KEY: Final = "capture_session_resolution"
NO_ACTIVE_SESSION_RESOLUTION: Final = "none_active"
# lab_tracker_client/watch.py: which rule gave a watched file its session;
# "active" is the checkout's `lt session use` context.
WATCH_SESSION_SOURCE_KEY: Final = "watch_session_source"
CHECKOUT_WATCH_SESSION_SOURCE: Final = "active"
# The voice debrief a session page offers (SessionDebrief.jsx).
CAPTURE_PURPOSE_KEY: Final = "capture_purpose"
SESSION_DEBRIEF_PURPOSE: Final = "session_debrief"
# lab_tracker_client/format_sniffers.py, copied into the note by lt watch.
FORMAT_SNIFF_ERROR_KEY: Final = "format_sniff_error"
H5PY_MISSING_SNIFF_ERROR: Final = "h5py not installed"

# Captures a gap needs before Lab Tracker's own check calls it detected.
# Sessionless app captures and watched files use the session-suggestion
# threshold; one memo or NWB file is already a clear gap; a lingering
# checkout session is never certain enough to call detected.
DETECTION_THRESHOLDS: Final[Mapping[CaptureSetupGap, int | None]] = MappingProxyType(
    {
        CaptureSetupGap.SESSIONLESS_APP_CAPTURES: MIN_CAPTURES_FOR_SESSION,
        CaptureSetupGap.SHORTCUT_NO_ACTIVE_SESSION: 1,
        CaptureSetupGap.SHORTCUT_WITHOUT_SESSION: 1,
        CaptureSetupGap.SESSIONLESS_WATCH_FILES: MIN_CAPTURES_FOR_SESSION,
        CaptureSetupGap.WATCH_SESSION_FROM_CHECKOUT: None,
        CaptureSetupGap.CLOSED_WITHOUT_DEBRIEF: MIN_CAPTURES_FOR_SESSION,
        CaptureSetupGap.NWB_HEADERS_UNREAD: 1,
    }
)

EXPLANATION_SOURCE_MODEL: Final = "model"
EXPLANATION_SOURCE_SERVER: Final = "server"
# An explanation that looks like a link or a command is replaced: the guide,
# not the model, names setup steps.
_UNSAFE_EXPLANATION: Final = re.compile(r"://|www\.|`|\blt\s+[a-z]", re.IGNORECASE)
_ELLIPSIS: Final = "…"


def _metadata(note: Note, key: str) -> str:
    return str(note.metadata.get(key) or "").strip()


def _is_watch_capture(note: Note) -> bool:
    return _metadata(note, EVIDENCE_ADAPTER_KEY).startswith(WATCH_ADAPTER_PREFIX)


def _is_app_capture(note: Note) -> bool:
    return (
        _metadata(note, CAPTURE_SOURCE_KEY) in APP_CAPTURE_SOURCES
        and _metadata(note, CAPTURE_CHANNEL_KEY) not in NON_APP_CAPTURE_CHANNELS
    )


def _is_thin(note: Note) -> bool:
    return len((note.transcribed_text or note.raw_content or "").strip()) <= THIN_CAPTURE_MAX_CHARS


def _detected(gap: CaptureSetupGap, count: int) -> bool:
    threshold = DETECTION_THRESHOLDS[gap]
    return threshold is not None and count >= threshold


def _candidate(
    gap: CaptureSetupGap,
    notes: Sequence[Note],
    *,
    detected: bool,
    session: Session | None = None,
) -> dict[str, Any]:
    session_id = str(session.session_id) if session is not None else None
    return {
        "candidate_id": candidate_id_for(gap, session_id),
        "kind": CAPTURE_SETUP_GUIDES[gap].kind.value,
        "gap": gap.value,
        "detected": detected,
        "note_ids": [str(note.note_id) for note in notes[:MAX_NOTE_IDS]],
        "note_count": len(notes),
        "session_id": session_id,
        "session_label": session_label(session) if session is not None else None,
    }


def _outside_own_sessions(
    notes: Sequence[Note], sessions: Sequence[Session], *, now: datetime
) -> list[Note]:
    """The captures no session of their author (or of no recorded author) contains.

    This is what session suggestions call sessionless. A capture inside two
    such sessions is unplaced (time cannot pick one) but not outside them.
    """

    if not notes:
        return []
    times = [capture_clock(note).at for note in notes]
    timeline = SessionTimeline(sessions, now=now, since=min(times))
    return [
        note
        for note, open_sessions in zip(notes, timeline.containing_many(times), strict=True)
        if not eligible_sessions(note, open_sessions)
    ]


def _batch_gap_notes(
    own: Sequence[Note],
    sessions: Sequence[Session],
    placement: Mapping[UUID, UUID | None],
    *,
    now: datetime,
) -> dict[CaptureSetupGap, list[Note]]:
    """The owner's captures behind each gap that is not tied to one session.

    Phone and web captures count only when made outside every session of
    their author, as the tip says; the other gaps need only an unplaced capture.
    """

    unplaced = [
        note for note in own if is_sessionless_capture(note) and placement.get(note.note_id) is None
    ]
    shortcut = [
        note for note in unplaced if _metadata(note, CAPTURE_CHANNEL_KEY) == SHORTCUT_CHANNEL
    ]
    app = [note for note in unplaced if _is_app_capture(note)]
    return {
        CaptureSetupGap.SESSIONLESS_APP_CAPTURES: _outside_own_sessions(app, sessions, now=now),
        CaptureSetupGap.SHORTCUT_NO_ACTIVE_SESSION: [
            note
            for note in shortcut
            if _metadata(note, CAPTURE_SESSION_RESOLUTION_KEY) == NO_ACTIVE_SESSION_RESOLUTION
        ],
        CaptureSetupGap.SHORTCUT_WITHOUT_SESSION: [
            note for note in shortcut if not _metadata(note, CAPTURE_SESSION_RESOLUTION_KEY)
        ],
        CaptureSetupGap.SESSIONLESS_WATCH_FILES: [
            note for note in unplaced if _is_watch_capture(note)
        ],
        CaptureSetupGap.WATCH_SESSION_FROM_CHECKOUT: [
            note
            for note in own
            if _is_watch_capture(note)
            and _metadata(note, WATCH_SESSION_SOURCE_KEY) == CHECKOUT_WATCH_SESSION_SOURCE
        ],
        CaptureSetupGap.NWB_HEADERS_UNREAD: [
            note
            for note in own
            if _metadata(note, FORMAT_SNIFF_ERROR_KEY) == H5PY_MISSING_SNIFF_ERROR
        ],
    }


def _debrief_candidates(
    batch_notes: Sequence[Note],
    own: Sequence[Note],
    sessions: Sequence[Session],
    placement: Mapping[UUID, UUID | None],
    *,
    owner_user_id: UUID,
    now: datetime,
) -> list[dict[str, Any]]:
    """Own closed sessions whose placed bench captures have no debrief in the batch."""

    bench: dict[UUID, list[Note]] = defaultdict(list)
    for note in own:
        session_id = placement.get(note.note_id)
        if session_id is not None and bench_capture_kind(note) is not None:
            bench[session_id].append(note)
    debriefed = {
        session_id
        for note in batch_notes
        if _metadata(note, CAPTURE_PURPOSE_KEY) == SESSION_DEBRIEF_PURPOSE
        for session_id in session_targets(note)
    }
    closed = [
        session
        for session in sessions
        if session.session_id in bench
        and session.session_id not in debriefed
        and session.created_by_user_id == owner_user_id
        and session.status == SessionStatus.CLOSED
        and session.ended_at is not None
        and as_utc(session.ended_at) <= as_utc(now)
    ]
    closed.sort(key=_newest_ended_first)
    gap = CaptureSetupGap.CLOSED_WITHOUT_DEBRIEF
    return [
        _candidate(
            gap,
            bench[session.session_id],
            detected=_detected(gap, sum(_is_thin(note) for note in bench[session.session_id])),
            session=session,
        )
        for session in closed[:MAX_DEBRIEF_SESSIONS]
    ]


def _newest_ended_first(session: Session) -> tuple[float, str]:
    ended_at = session.ended_at if session.ended_at is not None else session.started_at
    return -as_utc(ended_at).timestamp(), str(session.session_id)


def detect_capture_setup_candidates(
    notes: Sequence[Note],
    sessions: Sequence[Session],
    *,
    owner_user_id: UUID | None,
    now: datetime,
    suppressed: Collection[CaptureSetupKind] = frozenset(),
) -> list[dict[str, Any]]:
    """Capture-setup candidates for one batch's reviewer, in a fixed order.

    Only the owner's own staged captures are examined, and only a
    user-backed owner gets candidates (the cooldown is user-scoped). Each gap
    gives at most one candidate, in gap order; then up to
    :data:`MAX_DEBRIEF_SESSIONS` debrief candidates, newest ``ended_at``
    first. Kinds in ``suppressed`` are left out before the
    :data:`MAX_CANDIDATES` cap, which therefore drops the oldest debrief
    sessions rather than a whole gap. Note ids are in capture order, at most
    :data:`MAX_NOTE_IDS` per candidate; ``note_count`` is the full count.
    """

    if owner_user_id is None:
        return []
    own = sorted(
        (
            note
            for note in notes
            if note.created_by_user_id == owner_user_id and note.status == NoteStatus.STAGED
        ),
        key=lambda note: (capture_clock(note).at, str(note.note_id)),
    )
    if not own:
        return []
    placement = sessions_for_captures(own, sessions, now=now)
    gap_notes = _batch_gap_notes(own, sessions, placement, now=now)
    candidates = [
        _candidate(gap, gap_notes[gap], detected=_detected(gap, len(gap_notes[gap])))
        for gap in CaptureSetupGap
        if gap_notes.get(gap)
    ]
    candidates.extend(
        _debrief_candidates(notes, own, sessions, placement, owner_user_id=owner_user_id, now=now)
    )
    kept = [
        candidate
        for candidate in candidates
        if CaptureSetupKind(candidate["kind"]) not in suppressed
    ]
    return kept[:MAX_CANDIDATES]


def _recorded_kinds(packet: Mapping[str, Any]) -> set[CaptureSetupKind]:
    result = packet.get(RESULT_PACKET_KEY)
    recommendations = result.get("recommendations") if isinstance(result, dict) else None
    if not isinstance(recommendations, list):
        return set()
    kinds: set[CaptureSetupKind] = set()
    for item in recommendations:
        if not isinstance(item, dict):
            continue
        try:
            kinds.add(CaptureSetupKind(item.get("kind")))
        except ValueError:
            continue
    return kinds


def recently_recommended_kinds(
    change_sets: Iterable[GraphChangeSet],
    now: datetime,
) -> frozenset[CaptureSetupKind]:
    """Kinds recommended on batch drafts created within the cooldown before ``now``.

    The caller passes the reviewer's own change sets for the project (review
    memory already loads them); note-scoped drafts never carry tips, and a
    malformed packet or unknown kind is ignored.
    """

    cutoff = as_utc(now) - timedelta(days=COOLDOWN_DAYS)
    recent = (
        change_set
        for change_set in change_sets
        if change_set.draft_mode == GraphDraftMode.GRAPH_BATCH
        and as_utc(change_set.created_at) >= cutoff
    )
    return frozenset(
        kind for change_set in recent for kind in _recorded_kinds(change_set.context_packet)
    )


def _cited_note_ids(candidate: Mapping[str, Any], value: object) -> list[str]:
    """The candidate's own note ids the pick cites, in candidate order."""

    if not isinstance(value, list):
        return []
    cited: set[str] = set()
    for item in value:
        try:
            cited.add(str(UUID(str(item))))
        except ValueError:
            continue
    return [note_id for note_id in candidate["note_ids"] if note_id in cited]


def _explanation(value: object, server_sentence: str) -> tuple[str, str]:
    """``(text, source)``: the drafter's words, collapsed and cut, else the server's."""

    raw = "" if value is None else str(value)
    printable = "".join(character if character.isprintable() else " " for character in raw)
    text = " ".join(printable.split())
    if not text or _UNSAFE_EXPLANATION.search(text):
        return server_sentence, EXPLANATION_SOURCE_SERVER
    if len(text) > EXPLANATION_MAX_CHARS:
        text = text[: EXPLANATION_MAX_CHARS - len(_ELLIPSIS)].rstrip() + _ELLIPSIS
    return text, EXPLANATION_SOURCE_MODEL


def _guide_snapshot(gap: CaptureSetupGap, session_id: str | None) -> dict[str, Any]:
    guide = CAPTURE_SETUP_GUIDES[gap]
    app_path = guide.app_path
    if app_path is not None and SESSION_ID_PLACEHOLDER in app_path:
        app_path = app_path.replace(SESSION_ID_PLACEHOLDER, session_id) if session_id else None
    return {
        "title": guide.title,
        "steps": list(guide.steps),
        "app_path": app_path,
        "command": guide.command,
        "doc": guide.doc,
    }


def _recommendation(candidate: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any] | None:
    note_ids = _cited_note_ids(candidate, item.get("note_ids"))
    if not note_ids:
        return None
    gap = CaptureSetupGap(candidate["gap"])
    server_sentence = CAPTURE_SETUP_GUIDES[gap].server_explanation.format(count=len(note_ids))
    explanation, source = _explanation(item.get("explanation"), server_sentence)
    return {
        "recommendation_id": candidate["candidate_id"],
        "kind": candidate["kind"],
        "gap": candidate["gap"],
        "detected": candidate["detected"],
        "note_ids": note_ids,
        "note_count": len(note_ids),
        "session_id": candidate["session_id"],
        "session_label": candidate["session_label"],
        "explanation": explanation,
        "explanation_source": source,
        "guide": _guide_snapshot(gap, candidate["session_id"]),
    }


def _picked_candidate(unpicked: dict[str, dict[str, Any]], item: object) -> dict[str, Any] | None:
    """The offered candidate ``item`` names, removed so a repeat finds nothing."""

    candidate_id = item.get("candidate_id") if isinstance(item, dict) else None
    return unpicked.pop(candidate_id, None) if isinstance(candidate_id, str) else None


def resolve_capture_setup(candidates: object, items: object) -> dict[str, Any] | None:
    """The tips to record from the offered ``candidates`` and the drafter's ``items``.

    A pick is kept only when it is a dict naming an offered, not yet picked
    candidate and citing at least one of that candidate's notes; at most
    :data:`MAX_RECOMMENDATIONS` are kept and every other item counts as
    dropped. A missing or non-list ``items`` counts as nothing returned (there
    is no fallback tip). ``None`` when nothing was offered and nothing came back.
    """

    offered = trusted_candidates(candidates)
    returned = items if isinstance(items, list) else []
    if not offered and not returned:
        return None
    unpicked = {candidate["candidate_id"]: candidate for candidate in offered}
    recommendations: list[dict[str, Any]] = []
    for item in returned:
        candidate = _picked_candidate(unpicked, item)
        recommendation = _recommendation(candidate, item) if candidate is not None else None
        if recommendation is not None and len(recommendations) < MAX_RECOMMENDATIONS:
            recommendations.append(recommendation)
    return {
        "version": CAPTURE_SETUP_VERSION,
        "offered": [candidate["candidate_id"] for candidate in offered],
        "returned": len(returned),
        "dropped": len(returned) - len(recommendations),
        "recommendations": recommendations,
    }


def record_capture_setup(change_set: GraphChangeSet, graph_patch: Mapping[str, Any]) -> None:
    """Write this batch's tips into ``change_set.context_packet``; never raises.

    Best effort by contract: any failure is logged with the change set id and
    the change set is left exactly as it was.
    """

    try:
        result = resolve_capture_setup(
            change_set.context_packet.get(CANDIDATES_PACKET_KEY),
            graph_patch.get(RESPONSE_FIELD),
        )
    except Exception:
        logger.exception("capture-setup tips failed for change set %s", change_set.change_set_id)
        return
    if result is not None:
        change_set.context_packet[RESULT_PACKET_KEY] = result


__all__ = [
    "APP_CAPTURE_SOURCES",
    "DETECTION_THRESHOLDS",
    "EXPLANATION_SOURCE_MODEL",
    "EXPLANATION_SOURCE_SERVER",
    "NON_APP_CAPTURE_CHANNELS",
    "detect_capture_setup_candidates",
    "recently_recommended_kinds",
    "record_capture_setup",
    "resolve_capture_setup",
]
