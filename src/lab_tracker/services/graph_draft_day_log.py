"""Deterministic day-log grouping for batch drafts (no model involved).

A heavy bench day turns into many small captures -- a typed "added 5 ml
buffer", a dictated observation, a photo of a gel -- and the batch drafter
proposes something for each. This stage adds ONE extra proposal per session
that had at least :data:`DAY_LOG_MIN_CAPTURES` short bench captures in the
batch: a ``create_note`` whose body is a timestamped log of those captures
(``HH:MM — <first line of text/transcript, or file name>``), targeting the
session. A reviewer can accept one legible log instead of N items; the
individual capture proposals stay in the draft untouched.

It is honest about what it is:

* the rationale says it is a deterministic grouping, not model output, and it
  carries no confidence score;
* every capture is a ``source_refs`` entry (explicit ``source_note_ids``);
* the change set records each log it appended under
  ``context_packet["day_logs"]`` (a server-written field the model cannot
  set), which is how the applier stamps the committed note's
  ``origin_provider``/``origin_model`` as Lab Tracker's deterministic grouper
  rather than the batch's model, and how the review page labels it;
* the note's own metadata names the generator, session, and capture count.

It sends nothing anywhere: it reads only the batch's own staged notes and the
project's sessions, so the external-context policy has nothing to govern here.
Under delegated curation it is an ordinary ``create_note`` proposal, admitted
by ``full`` and never by ``organize``. It is idempotent: the change set is
keyed by the batch, and a log whose ``day_log_key`` (session plus the exact
capture set) already exists as a note is not proposed again.

"Short bench capture" means a note a person made at the bench, not an
adapter's import: no ``evidence_source_provider``, not a booking or an
onboarding checkpoint, and one of

* text: no uploaded file and at most :data:`SHORT_CAPTURE_MAX_CHARS` of text;
* voice: an ``audio/*`` upload with a transcript of at most that length;
* photo: an ``image/*`` upload.

A capture's session is its single declared session target, else the session
id its metadata names, else the one session open at its capture time.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from enum import Enum
from typing import Any, Final, Protocol
from uuid import UUID, uuid4

from lab_tracker.errors import ValidationError
from lab_tracker.member_onboarding import is_member_checkpoint
from lab_tracker.models import (
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeSet,
    GraphDraftSemanticType,
    Note,
    NoteStatus,
    Session,
    utc_now,
)
from lab_tracker.services.session_clock import (
    BatchSettingsReader,
    capture_clock,
    is_booking_note,
    resolve_capture_timezone,
    session_for_capture,
    session_label,
)

logger = logging.getLogger(__name__)

DAY_LOG_MIN_CAPTURES: Final = 3
SHORT_CAPTURE_MAX_CHARS: Final = 1000
# Bounds on what the log copies: characters per line, lines per log.
DAY_LOG_LINE_MAX_CHARS: Final = 120
DAY_LOG_MAX_ENTRIES: Final = 200

DAY_LOG_PACKET_KEY: Final = "day_logs"
DAY_LOG_GENERATOR: Final = "lab_tracker.day_log/v1"
# What the applier stamps on a committed day log instead of the batch's model.
DAY_LOG_ORIGIN_PROVIDER: Final = "lab_tracker"
DAY_LOG_ORIGIN_MODEL: Final = "deterministic_day_log"
DAY_LOG_ORIGIN_PROMPT_VERSION: Final = "day_log/v1"

DAY_LOG_KEY_METADATA: Final = "day_log_key"
DAY_LOG_SESSION_METADATA: Final = "day_log_session_id"
DAY_LOG_COUNT_METADATA: Final = "day_log_capture_count"
DAY_LOG_GENERATOR_METADATA: Final = "day_log_generator"
DAY_LOG_TIMEZONE_METADATA: Final = "day_log_timezone"

_ADAPTER_METADATA_KEY: Final = "evidence_source_provider"
_ELLIPSIS: Final = "…"


class BenchCaptureKind(str, Enum):
    """The three short bench captures a day log folds together."""

    TEXT = "text"
    VOICE = "voice"
    PHOTO = "photo"


@dataclass(frozen=True)
class DayLogEntry:
    """One line of a day log."""

    note_id: UUID
    at: datetime
    kind: BenchCaptureKind
    text: str


@dataclass(frozen=True)
class DayLogPlan:
    """The captures one session's day log will list, in capture order."""

    session: Session
    entries: tuple[DayLogEntry, ...]
    key: str


def _short(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and len(stripped) <= SHORT_CAPTURE_MAX_CHARS


def bench_capture_kind(note: Note) -> BenchCaptureKind | None:
    """The kind of short bench capture ``note`` is, or ``None``."""

    metadata = note.metadata
    if (
        note.status != NoteStatus.STAGED
        or is_member_checkpoint(note)
        or str(metadata.get(_ADAPTER_METADATA_KEY) or "").strip()
        or str(metadata.get(DAY_LOG_KEY_METADATA) or "").strip()
        or is_booking_note(note)
    ):
        return None
    asset = note.raw_asset
    if asset is None:
        return BenchCaptureKind.TEXT if _short(note.raw_content) else None
    content_type = (asset.content_type or "").lower()
    if content_type.startswith("image/"):
        return BenchCaptureKind.PHOTO
    if content_type.startswith("audio/") and _short(note.transcribed_text or ""):
        return BenchCaptureKind.VOICE
    return None


def _first_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = " ".join(line.split())
        if cleaned:
            return cleaned
    return ""


def _bounded(text: str) -> str:
    if len(text) <= DAY_LOG_LINE_MAX_CHARS:
        return text
    return text[: DAY_LOG_LINE_MAX_CHARS - 1].rstrip() + _ELLIPSIS


def capture_line_text(note: Note, kind: BenchCaptureKind) -> str:
    """What a capture contributes to its log line (bounded, single line)."""

    if kind == BenchCaptureKind.PHOTO:
        filename = note.raw_asset.filename if note.raw_asset is not None else ""
        text = _first_line(filename or "") or "photo"
    elif kind == BenchCaptureKind.VOICE:
        text = _first_line(note.transcribed_text or "") or "voice note"
    else:
        text = _first_line(note.raw_content)
    return _bounded(text)


def day_log_key(project_id: UUID, session_id: UUID, note_ids: Iterable[UUID]) -> str:
    """Identity of one day log: the project, the session, and the exact captures."""

    material = "|".join([str(project_id), str(session_id), *sorted(str(item) for item in note_ids)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def plan_day_logs(
    notes: Iterable[Note],
    sessions: Sequence[Session],
    *,
    now: datetime,
    existing_keys: set[str] | frozenset[str] = frozenset(),
    min_captures: int = DAY_LOG_MIN_CAPTURES,
) -> list[DayLogPlan]:
    """One plan per session with at least ``min_captures`` short bench captures.

    Plans whose key is in ``existing_keys`` (a log already recorded as a note)
    are dropped. Plans come back in session start order.
    """

    by_id = {session.session_id: session for session in sessions}
    grouped: dict[UUID, list[DayLogEntry]] = defaultdict(list)
    for note in notes:
        kind = bench_capture_kind(note)
        if kind is None:
            continue
        session_id = session_for_capture(note, sessions, now=now)
        if session_id is None or session_id not in by_id:
            continue
        grouped[session_id].append(
            DayLogEntry(
                note_id=note.note_id,
                at=capture_clock(note).at,
                kind=kind,
                text=capture_line_text(note, kind),
            )
        )
    plans: list[DayLogPlan] = []
    for session_id, entries in grouped.items():
        if len(entries) < min_captures:
            continue
        session = by_id[session_id]
        ordered = tuple(sorted(entries, key=lambda entry: (entry.at, str(entry.note_id))))
        key = day_log_key(session.project_id, session_id, (entry.note_id for entry in ordered))
        if key in existing_keys:
            continue
        plans.append(DayLogPlan(session=session, entries=ordered, key=key))
    return sorted(plans, key=lambda plan: (plan.session.started_at, str(plan.session.session_id)))


def day_log_body(plan: DayLogPlan, *, zone: tzinfo, zone_name: str) -> str:
    """The log text: a header, then ``HH:MM — text`` per capture, day-headed.

    When the captures span more than one local day each day gets its own
    ``YYYY-MM-DD`` heading so the clock times stay unambiguous. At most
    :data:`DAY_LOG_MAX_ENTRIES` lines are written; the rest are counted.
    """

    days = [entry.at.astimezone(zone).date() for entry in plan.entries]
    first_day = days[0].isoformat()
    last_day = days[-1].isoformat()
    span = first_day if first_day == last_day else f"{first_day} to {last_day}"
    lines = [f"Day log — {session_label(plan.session)}, {span} ({zone_name})", ""]
    multi_day = first_day != last_day
    current_day = None
    shown = plan.entries[:DAY_LOG_MAX_ENTRIES]
    for entry, day in zip(shown, days, strict=False):
        if multi_day and day != current_day:
            if current_day is not None:
                lines.append("")
            lines.append(day.isoformat())
            current_day = day
        lines.append(f"{entry.at.astimezone(zone).strftime('%H:%M')} — {entry.text}")
    hidden = len(plan.entries) - len(shown)
    if hidden > 0:
        lines.append(f"… and {hidden} more capture{'s' if hidden != 1 else ''}")
    return "\n".join(lines)


def day_log_rationale(plan: DayLogPlan) -> str:
    count = len(plan.entries)
    return (
        f"grouped {count} captures from {session_label(plan.session)}. "
        "Deterministic day-log grouping by Lab Tracker, not model output; "
        "the individual capture proposals remain in this draft."
    )


def day_log_operation(
    plan: DayLogPlan,
    *,
    change_set_id: UUID,
    sequence: int,
    zone: tzinfo,
    zone_name: str,
) -> GraphChangeOperation:
    """The proposed ``create_note`` for one plan (status PROPOSED)."""

    session = plan.session
    first_day = plan.entries[0].at.astimezone(zone).date().isoformat()
    payload: dict[str, Any] = {
        "project_id": str(session.project_id),
        "raw_content": day_log_body(plan, zone=zone, zone_name=zone_name),
        "targets": [
            {"entity_type": EntityType.SESSION.value, "entity_id": str(session.session_id)}
        ],
        # A reviewer's accept makes it part of the record; it is not a new
        # capture for the next batch to draft over again.
        "status": NoteStatus.COMMITTED.value,
        "metadata": {
            "title": f"Day log · {first_day} · {session_label(session)}",
            DAY_LOG_GENERATOR_METADATA: DAY_LOG_GENERATOR,
            DAY_LOG_KEY_METADATA: plan.key,
            DAY_LOG_SESSION_METADATA: str(session.session_id),
            DAY_LOG_COUNT_METADATA: str(len(plan.entries)),
            DAY_LOG_TIMEZONE_METADATA: zone_name,
        },
    }
    return GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=change_set_id,
        sequence=sequence,
        op=GraphChangeOp.CREATE,
        entity_type=EntityType.NOTE,
        semantic_type=GraphDraftSemanticType.CREATE_NOTE,
        payload=payload,
        client_ref=f"day_log_{plan.key[:12]}",
        rationale=day_log_rationale(plan),
        confidence=None,
        source_refs=[
            {
                "label": f"{entry.at.astimezone(zone).strftime('%H:%M')} {entry.kind.value}",
                "quote": entry.text,
                "region": None,
                "source_note_ids": [str(entry.note_id)],
                "source_note_ids_resolution": "explicit",
            }
            for entry in plan.entries
        ],
    )


def day_log_packet_entry(plan: DayLogPlan, operation: GraphChangeOperation) -> dict[str, Any]:
    """What the change set records about one appended log."""

    return {
        "operation_id": str(operation.operation_id),
        "generator": DAY_LOG_GENERATOR,
        "origin": "deterministic",
        "session_id": str(plan.session.session_id),
        "day_log_key": plan.key,
        "capture_note_ids": [str(entry.note_id) for entry in plan.entries],
    }


def recorded_day_log_operation_ids(change_set: GraphChangeSet) -> set[str]:
    """Operation ids the change set recorded as deterministic day logs."""

    packet = change_set.context_packet if isinstance(change_set.context_packet, dict) else {}
    entries = packet.get(DAY_LOG_PACKET_KEY)
    if not isinstance(entries, list):
        return set()
    return {
        str(entry.get("operation_id"))
        for entry in entries
        if isinstance(entry, dict) and entry.get("operation_id")
    }


def is_day_log_operation(change_set: GraphChangeSet, operation: GraphChangeOperation) -> bool:
    """True for an operation this stage appended (per the server-written packet)."""

    return str(operation.operation_id) in recorded_day_log_operation_ids(change_set)


class _IdentifierCarriers(Protocol):
    def list_identifier_carriers(self, project_id: UUID, keys: Sequence[str]) -> list[Note]: ...


class DayLogValidator(Protocol):
    def validate_operation(
        self,
        operation: GraphChangeOperation,
        payload: dict[str, Any],
    ) -> None: ...


class DayLogOwner(Protocol):
    @property
    def reviewer_user_id(self) -> UUID | None: ...


class DayLogRepository(BatchSettingsReader, Protocol):
    """The reads the day-log stage needs: sessions, recorded logs, the zone."""

    def query_sessions(
        self,
        *,
        project_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Session], int]: ...

    @property
    def provenance_links(self) -> _IdentifierCarriers: ...


def with_day_log_proposals(
    change_set: GraphChangeSet,
    notes: Sequence[Note],
    operations: list[GraphChangeOperation],
    repository: DayLogRepository,
    validator: DayLogValidator,
    owner: DayLogOwner | None,
) -> list[GraphChangeOperation]:
    """``operations`` plus one validated day log per qualifying session.

    ``owner`` is the batch's context owner (its reviewer), whose daily-review
    zone labels the log; ``validator`` checks each log like a model proposal.

    Best effort by contract: any failure here is logged and the model's
    operations come back unchanged, so the stage can never fail a batch. Each
    appended log is recorded under ``change_set.context_packet["day_logs"]``.
    Open sessions are measured to the batch window's end (else now), so a
    retried generation of the same batch plans the same logs.
    """

    try:
        sessions, _total = repository.query_sessions(
            project_id=change_set.project_id, limit=None, offset=0
        )
        if not sessions:
            return operations
        recorded = repository.provenance_links.list_identifier_carriers(
            change_set.project_id, (DAY_LOG_KEY_METADATA,)
        )
        existing_keys = {str(note.metadata.get(DAY_LOG_KEY_METADATA)) for note in recorded}
        plans = plan_day_logs(
            notes,
            sessions,
            now=change_set.batch_window_end or utc_now(),
            existing_keys=existing_keys,
        )
        if not plans:
            return operations
        owner_user_id = owner.reviewer_user_id if owner is not None else None
        zone, zone_name = resolve_capture_timezone(repository, change_set.project_id, owner_user_id)
        appended: list[GraphChangeOperation] = []
        entries: list[dict[str, Any]] = []
        sequence = max((operation.sequence for operation in operations), default=0)
        for plan in plans:
            operation = day_log_operation(
                plan,
                change_set_id=change_set.change_set_id,
                sequence=sequence + 1,
                zone=zone,
                zone_name=zone_name,
            )
            try:
                validator.validate_operation(operation, operation.payload)
            except ValidationError:
                logger.warning(
                    "day log for session %s failed validation; skipped",
                    plan.session.session_id,
                    exc_info=True,
                )
                continue
            sequence += 1
            appended.append(operation)
            entries.append(day_log_packet_entry(plan, operation))
    except Exception:
        logger.exception("day-log grouping failed for change set %s", change_set.change_set_id)
        return operations
    if entries:
        change_set.context_packet[DAY_LOG_PACKET_KEY] = entries
    return [*operations, *appended]


__all__ = [
    "DayLogOwner",
    "DayLogRepository",
    "DayLogValidator",
    "DAY_LOG_GENERATOR",
    "DAY_LOG_KEY_METADATA",
    "DAY_LOG_MAX_ENTRIES",
    "DAY_LOG_MIN_CAPTURES",
    "DAY_LOG_ORIGIN_MODEL",
    "DAY_LOG_ORIGIN_PROMPT_VERSION",
    "DAY_LOG_ORIGIN_PROVIDER",
    "DAY_LOG_PACKET_KEY",
    "SHORT_CAPTURE_MAX_CHARS",
    "BenchCaptureKind",
    "DayLogEntry",
    "DayLogPlan",
    "bench_capture_kind",
    "capture_line_text",
    "day_log_body",
    "day_log_key",
    "day_log_operation",
    "day_log_packet_entry",
    "day_log_rationale",
    "is_day_log_operation",
    "plan_day_logs",
    "recorded_day_log_operation_ids",
    "with_day_log_proposals",
]
