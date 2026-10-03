"""Capture-setup tips: the closed catalog the server and the batch drafter share.

A batch draft can end with forward-looking advice for the person whose
captures it read: "these memos reached no session; have one open when you
dictate". The server finds the gaps from capture metadata, offers them to the
drafter as *candidates*, and the drafter picks the ones whose captures it
could not interpret and explains each in its own words. Everything else a tip
shows -- its title, setup steps, the app page to open, an optional ``lt``
command, and the guide to read -- is copy owned here, so the model can never
name a setup step, a command, a menu, or a feature that does not exist. The
drift tests in ``tests/test_docs_drift.py`` pin every doc anchor, ``lt``
command, and UI label this copy quotes.

Five setup kinds close seven gaps (:data:`CAPTURE_SETUP_GUIDES` maps each gap
to its guide). A candidate carries only server values -- enum names, note and
session ids, counts, and the session's own label -- never capture text, and
:func:`trusted_candidates` is the whitelist the prompt renders from.

This module is stdlib-only so the provider clients and the services can both
import it without a cycle.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Final
from uuid import UUID

CAPTURE_SETUP_VERSION: Final = "capture_setup/v1"
# context_packet keys: the server-derived input, and the server-written result.
CANDIDATES_PACKET_KEY: Final = "capture_setup_candidates"
RESULT_PACKET_KEY: Final = "capture_setup"
# The drafter's response field (batch drafts only).
RESPONSE_FIELD: Final = "capture_setup_recommendations"

# Bounds: candidates offered per batch, debrief sessions among them, note ids
# listed per candidate, and tips kept per draft.
MAX_CANDIDATES: Final = 8
MAX_DEBRIEF_SESSIONS: Final = 3
MAX_NOTE_IDS: Final = 20
MAX_RECOMMENDATIONS: Final = 6
# The drafter's explanation is cut to this many characters.
EXPLANATION_MAX_CHARS: Final = 280
# A bench capture whose own text (transcript, else typed text) is at most this
# long does not explain itself.
THIN_CAPTURE_MAX_CHARS: Final = 40
# A kind recommended to a reviewer in a project is not offered again for this long.
COOLDOWN_DAYS: Final = 7


class CaptureSetupKind(str, Enum):
    """A capture setup a person can use so future captures carry what was missing."""

    SESSION_CAPTURE_LINK = "session_capture_link"
    SHORTCUT_SESSION = "shortcut_session"
    WATCH_FOLDER_LINK_CODE = "watch_folder_link_code"
    SESSION_DEBRIEF = "session_debrief"
    NWB_H5PY = "nwb_h5py"


class CaptureSetupGap(str, Enum):
    """What some of a person's batch captures lacked, as the server detects it."""

    SESSIONLESS_APP_CAPTURES = "sessionless_app_captures"
    SHORTCUT_NO_ACTIVE_SESSION = "shortcut_no_active_session"
    SHORTCUT_WITHOUT_SESSION = "shortcut_without_session"
    SESSIONLESS_WATCH_FILES = "sessionless_watch_files"
    WATCH_SESSION_FROM_CHECKOUT = "watch_session_from_checkout"
    CLOSED_WITHOUT_DEBRIEF = "closed_without_debrief"
    NWB_HEADERS_UNREAD = "nwb_headers_unread"


# Gaps found per session rather than per batch: their candidate names the
# session, and their guide opens its page.
SESSION_SCOPED_GAPS: Final = frozenset({CaptureSetupGap.CLOSED_WITHOUT_DEBRIEF})
SESSION_ID_PLACEHOLDER: Final = "{session_id}"


@dataclass(frozen=True)
class CaptureSetupGuide:
    """The server-owned copy for one gap.

    ``app_path`` may contain ``{session_id}``; ``doc`` is ``path#anchor``;
    each ``ui_labels`` entry is ``(label, component path under
    src/lab_tracker/frontend_src)`` for a label the steps quote in double
    quotes; ``server_explanation`` has a ``{count}`` placeholder and replaces
    a drafter explanation that is empty or unsafe.
    """

    kind: CaptureSetupKind
    title: str
    steps: tuple[str, ...]
    app_path: str | None
    command: str | None
    doc: str
    ui_labels: tuple[tuple[str, str], ...]
    server_explanation: str


_HOME: Final = ("Home", "shared/ui.jsx")
_DEVICES: Final = ("Devices", "shared/ui.jsx")
_START_SESSION: Final = ("Start session", "features/sessions/SessionPanel.jsx")
_LINK_CODE: Final = ("Link code", "features/sessions/SessionLinkCode.jsx")
_CAPTURE_LINK_SECTION: Final = "features/sessions/SessionCaptureLinkSection.jsx"
_SHORTCUT_PANEL: Final = "features/bench-capture/HandsFreeShortcutPanel.jsx"
_SHORTCUT_DOC: Final = "docs/bench-capture.md#hands-free-voice-shortcut"
_FOLDER_NAMES_DOC: Final = "docs/watch-folder-capture.md#sessions-from-folder-names"

CAPTURE_SETUP_GUIDES: Final[Mapping[CaptureSetupGap, CaptureSetupGuide]] = MappingProxyType(
    {
        CaptureSetupGap.SESSIONLESS_APP_CAPTURES: CaptureSetupGuide(
            kind=CaptureSetupKind.SESSION_CAPTURE_LINK,
            title="Capture into a session from the start",
            steps=(
                'When the bench work begins, start a session on "Home" with "Start session".',
                'While it is open, scan the session page\'s "Capture into this session" QR '
                "code with your phone; everything captured from that page arrives linked to "
                "the session.",
                'At the rig, "Write NFC tag" puts the same link on an NFC sticker (Chrome on '
                'Android), and "Open bench kiosk" opens a scan station for the session on a '
                "shared bench computer.",
            ),
            app_path="/app",
            command=None,
            doc="docs/phone-capture-quickstart.md#capture-at-the-bench",
            ui_labels=(
                _HOME,
                _START_SESSION,
                ("Capture into this session", _CAPTURE_LINK_SECTION),
                ("Write NFC tag", "features/bench-capture/NfcTagWriter.jsx"),
                ("Open bench kiosk", _CAPTURE_LINK_SECTION),
            ),
            server_explanation=(
                "Phone or web captures ({count}) named no session and were made outside every "
                "session you ran, so the drafter could not tell which bench work they belong to."
            ),
        ),
        CaptureSetupGap.SHORTCUT_NO_ACTIVE_SESSION: CaptureSetupGuide(
            kind=CaptureSetupKind.SHORTCUT_SESSION,
            title="Have a session open when you dictate",
            steps=(
                'Start a session on "Home" with "Start session" before you record memos with '
                "the hands-free shortcut.",
                "The shortcut files each memo into your most recently started open session, so "
                "memos recorded while it is open arrive linked to it.",
            ),
            app_path="/app",
            command=None,
            doc=_SHORTCUT_DOC,
            ui_labels=(_HOME, _START_SESSION),
            server_explanation=(
                "Hands-free shortcut memos ({count}) asked for your latest session while you had "
                "none open, so they arrived without a session."
            ),
        ),
        CaptureSetupGap.SHORTCUT_WITHOUT_SESSION: CaptureSetupGuide(
            kind=CaptureSetupKind.SHORTCUT_SESSION,
            title="Send your open session with each shortcut memo",
            steps=(
                'Open "Devices", find "Hands-free shortcut", choose the project, and use '
                '"Copy URL": that URL asks for your latest open session (session_id=latest).',
                "Paste it as the request URL in your phone shortcut, replacing the one it uses "
                "now.",
                'Start a session on "Home" with "Start session" before you dictate.',
            ),
            app_path="/app/devices",
            command=None,
            doc=_SHORTCUT_DOC,
            ui_labels=(
                _DEVICES,
                ("Hands-free shortcut", _SHORTCUT_PANEL),
                ("Copy URL", _SHORTCUT_PANEL),
                _HOME,
                _START_SESSION,
            ),
            server_explanation=(
                "Hands-free shortcut memos ({count}) were sent without a session, so the drafter "
                "could not tell which session they belong to."
            ),
        ),
        CaptureSetupGap.SESSIONLESS_WATCH_FILES: CaptureSetupGuide(
            kind=CaptureSetupKind.WATCH_FOLDER_LINK_CODE,
            title="Put the session's link code in the folder name",
            steps=(
                'Start a session on "Home" and copy its "Link code" (LT-<code>) from the '
                "session list or the session page.",
                "Inside the watched folder, save that session's files in a new folder whose "
                "name includes the code, for example session001_LT-<code>; every file saved "
                "inside it is then linked to the session.",
                "A folder that is not watched yet can be registered for one session instead: "
                "preview with the command below, then run it again without --dry-run.",
            ),
            app_path="/app",
            command="lt watch add <folder> --session LT-<code> --dry-run",
            doc=_FOLDER_NAMES_DOC,
            ui_labels=(_HOME, _LINK_CODE),
            server_explanation=(
                "Files synced from a watched folder ({count}) named no session, so the drafter "
                "could not tell which session produced them."
            ),
        ),
        CaptureSetupGap.WATCH_SESSION_FROM_CHECKOUT: CaptureSetupGuide(
            kind=CaptureSetupKind.WATCH_FOLDER_LINK_CODE,
            title="Name the session in the folder, not the checkout",
            steps=(
                "These files took their session from the checkout-wide session default, which "
                "claims every new file until it expires.",
                'Copy the session\'s "Link code" (LT-<code>) and save its files in a folder '
                "whose name includes it, for example session001_LT-<code>; a code in the "
                "folder name takes precedence over the checkout default.",
                "When the session ends, run the command below in that checkout so later files "
                "are not claimed by it.",
            ),
            app_path="/app",
            command="lt session clear",
            doc=_FOLDER_NAMES_DOC,
            ui_labels=(_LINK_CODE,),
            server_explanation=(
                "Files synced from a watched folder ({count}) took their session from a "
                "checkout-wide default rather than from the folder, so the drafter could not be "
                "sure they belong to it."
            ),
        ),
        CaptureSetupGap.CLOSED_WITHOUT_DEBRIEF: CaptureSetupGuide(
            kind=CaptureSetupKind.SESSION_DEBRIEF,
            title="Record a debrief for this session",
            steps=(
                'Open the session and use "Debrief": three prompts (what happened, what '
                "surprised you, what you would change) and one recording.",
                "The recording is saved as a voice note linked to the session and is drafted "
                "once it has a transcript.",
                "Next time, record the debrief offered when you close a session instead of "
                'choosing "Skip".',
            ),
            app_path=f"/app/sessions/{SESSION_ID_PLACEHOLDER}",
            command=None,
            doc="docs/bench-capture.md#voice-debrief",
            ui_labels=(
                ("Debrief", "features/sessions/SessionDetailCard.jsx"),
                ("Skip", "features/bench-capture/SessionDebrief.jsx"),
            ),
            server_explanation=(
                "Bench captures from this session ({count}) carry little context of their own, "
                "and the session has no debrief."
            ),
        ),
        CaptureSetupGap.NWB_HEADERS_UNREAD: CaptureSetupGuide(
            kind=CaptureSetupKind.NWB_H5PY,
            title="Install h5py where your folders are watched",
            steps=(
                "On the computer that watches the acquisition folder, install the h5py package "
                "into the Python environment that runs lt.",
                "NWB files synced after that carry the recording's start time, identifier, and "
                "subject id from their headers, which also places them in time against your "
                "sessions.",
            ),
            app_path=None,
            command=None,
            doc="docs/decoded-labels-and-file-headers.md#instrument-file-headers-lt-watch",
            ui_labels=(),
            server_explanation=(
                "NWB files ({count}) were synced without h5py on the capturing computer, so their "
                "start time, identifier, and subject were not read."
            ),
        ),
    }
)

# What each kind covers, for the drafter: no steps, commands, or links.
_KIND_PROMPT_DESCRIPTIONS: Final[Mapping[CaptureSetupKind, str]] = MappingProxyType(
    {
        CaptureSetupKind.SESSION_CAPTURE_LINK: (
            "phone, web capture page, share, or kiosk captures that named no session and were "
            "made outside every session the person ran (sessionless_app_captures)."
        ),
        CaptureSetupKind.SHORTCUT_SESSION: (
            "hands-free shortcut voice memos that reached no session, because none was open "
            "(shortcut_no_active_session) or the shortcut sent none (shortcut_without_session)."
        ),
        CaptureSetupKind.WATCH_FOLDER_LINK_CODE: (
            "files synced from a watched folder that named no session (sessionless_watch_files) "
            "or took it from a checkout-wide default rather than the folder "
            "(watch_session_from_checkout)."
        ),
        CaptureSetupKind.SESSION_DEBRIEF: (
            "a closed session the person ran whose bench captures carry little context and that "
            "has no debrief recording (closed_without_debrief)."
        ),
        CaptureSetupKind.NWB_H5PY: (
            "NWB files whose headers (start time, identifier, subject) were not read on the "
            "computer that synced them (nwb_headers_unread)."
        ),
    }
)

_CANDIDATE_KEYS: Final = (
    "candidate_id",
    "kind",
    "gap",
    "detected",
    "note_ids",
    "note_count",
    "session_id",
    "session_label",
)
# session_clock.session_label: "<session type> session LT-<link code>".
_SESSION_LABEL: Final = re.compile(r"[a-z]+ session LT-(?P<code>[A-Z2-7]+)")


def candidate_id_for(gap: CaptureSetupGap, session_id: UUID | str | None) -> str:
    """The stable id of a candidate: its gap, plus the session for a per-session gap."""

    return gap.value if session_id is None else f"{gap.value}:{session_id}"


def capture_setup_items_schema() -> dict[str, Any]:
    """The strict JSON schema of the drafter's ``capture_setup_recommendations`` array."""

    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string"},
                "note_ids": {"type": "array", "items": {"type": "string"}},
                "explanation": {"type": "string"},
            },
            "additionalProperties": False,
            "required": ["candidate_id", "note_ids", "explanation"],
        },
    }


def capture_setup_prompt_lines() -> list[str]:
    """One prompt line per kind: what its candidates cover, by gap name."""

    return [f"- {kind.value}: {_KIND_PROMPT_DESCRIPTIONS[kind]}" for kind in CaptureSetupKind]


def _link_code(session_id: UUID) -> str:
    # models.encode_session_link_code, restated so this module stays stdlib-only.
    return base64.b32encode(session_id.bytes).decode("ascii").rstrip("=")


def _uuid(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _note_ids(value: object) -> list[str] | None:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_NOTE_IDS:
        return None
    parsed = [_uuid(item) for item in value]
    if any(note_id is None for note_id in parsed):
        return None
    return list(dict.fromkeys(str(note_id) for note_id in parsed))


def _session(
    gap: CaptureSetupGap, session_value: object, label_value: object
) -> tuple[bool, str | None, str | None]:
    """``(valid, session_id, session_label)``: present exactly for per-session gaps."""

    if gap not in SESSION_SCOPED_GAPS:
        return session_value is None and label_value is None, None, None
    session_id = _uuid(session_value)
    if session_id is None or not isinstance(label_value, str):
        return False, None, None
    match = _SESSION_LABEL.fullmatch(label_value)
    if match is None or match["code"] != _link_code(session_id):
        return False, None, None
    return True, str(session_id), label_value


def _trusted_candidate(item: object) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    try:
        gap = CaptureSetupGap(item.get("gap"))
        kind = CaptureSetupKind(item.get("kind"))
    except ValueError:
        return None
    note_ids = _note_ids(item.get("note_ids"))
    note_count = item.get("note_count")
    detected = item.get("detected")
    valid_session, session_id, session_label = _session(
        gap, item.get("session_id"), item.get("session_label")
    )
    if (
        kind is not CAPTURE_SETUP_GUIDES[gap].kind
        or note_ids is None
        or not isinstance(detected, bool)
        or not isinstance(note_count, int)
        or isinstance(note_count, bool)
        or note_count < len(note_ids)
        or not valid_session
        or item.get("candidate_id") != candidate_id_for(gap, session_id)
    ):
        return None
    values = (
        candidate_id_for(gap, session_id),
        kind.value,
        gap.value,
        detected,
        note_ids,
        note_count,
        session_id,
        session_label,
    )
    return dict(zip(_CANDIDATE_KEYS, values, strict=True))


def trusted_candidates(value: object) -> list[dict[str, Any]]:
    """The candidates in ``value`` made only of whitelisted server values.

    Each kept candidate has exactly the known keys, a kind and gap from the
    enums (the kind the gap's guide names), canonical UUID strings, a bool
    ``detected``, and an int ``note_count``; a per-session gap carries its
    session id and that session's own ``LT-`` label. Anything else -- an
    unknown key, a malformed value, a repeated id -- is dropped, and at most
    :data:`MAX_CANDIDATES` are kept.
    """

    if not isinstance(value, list):
        return []
    trusted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        candidate = _trusted_candidate(item)
        if candidate is None or candidate["candidate_id"] in seen:
            continue
        seen.add(candidate["candidate_id"])
        trusted.append(candidate)
        if len(trusted) == MAX_CANDIDATES:
            break
    return trusted


__all__ = [
    "CANDIDATES_PACKET_KEY",
    "CAPTURE_SETUP_GUIDES",
    "CAPTURE_SETUP_VERSION",
    "COOLDOWN_DAYS",
    "EXPLANATION_MAX_CHARS",
    "MAX_CANDIDATES",
    "MAX_DEBRIEF_SESSIONS",
    "MAX_NOTE_IDS",
    "MAX_RECOMMENDATIONS",
    "RESPONSE_FIELD",
    "RESULT_PACKET_KEY",
    "SESSION_ID_PLACEHOLDER",
    "SESSION_SCOPED_GAPS",
    "THIN_CAPTURE_MAX_CHARS",
    "CaptureSetupGap",
    "CaptureSetupGuide",
    "CaptureSetupKind",
    "candidate_id_for",
    "capture_setup_items_schema",
    "capture_setup_prompt_lines",
    "trusted_candidates",
]
