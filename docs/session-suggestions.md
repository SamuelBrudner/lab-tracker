# Sessions as the clock

Bench work happens in time, and an acquisition session is the unit that time is
cut into. Starting, ending, and attaching captures to sessions is exactly the
bookkeeping a busy person forgets. Lab Tracker now lets **time do the
linking**, and keeps the review burden proportional to the work:

| Piece | What it does | Who decides |
| --- | --- | --- |
| **Time-window provenance** | Proposes `note -> session` links for captures made while exactly one session was open | A person accepts or rejects each link |
| **Session suggestions** | Notices a forgotten open session, a bench day with no session, an instrument booking no session covers | A person clicks Apply or Dismiss |
| **Day-log grouping** | Folds a session's short bench captures into one proposed timestamped log in the daily review | A reviewer accepts one log instead of N items |

None of the three writes a committed record on its own: the first writes
`PROPOSED` provenance links, the second writes nothing at all, the third
appends one `proposed` operation to a daily-review draft. Delegated curation
([delegated-curation.md](delegated-curation.md)) treats the day log like any
other `create_note` proposal (admitted by `full`, never by `organize`).

## The capture clock and the session window

All three read the same two facts
(`src/lab_tracker/services/session_clock.py`):

- **When a capture happened.** The instrument's acquisition time
  `format_acquired_at` (written by the file-format decoders, ISO-8601 UTC; a
  value without an offset is read as UTC) wins when present and parseable.
  Otherwise the note's observed time: client `captured_at`, then the adapter's
  `evidence_source_observed_at`, then the server's `created_at`. Either way the
  value is clamped to `created_at`.
- **Which session was open.** A session's window is `[started_at, ended_at]`,
  or `[started_at, now]` while it is open, inclusive at both ends. A capture
  that falls inside two overlapping windows is **ambiguous**, and every rule
  here then does nothing.

A note "already has a session" when it carries a session target or any of
`watch_session_id`, `capture_session_id`, `photo_session_id`, or
`decoded_session_link_code`. Time never second-guesses a session the capture
itself named.

Local days and `HH:MM` labels use the zone the daily review runs in: the
person's own daily-review settings row, then the project-default row
(`timezone_name`, which starts as `America/New_York` once the row exists), else
UTC.

## Time-window provenance proposals

Every batch execution (Run now, the queued worker, due dispatch) runs the
time-window detector after the content-hash and exact-id detectors, best
effort, before the model drafts. For each note created in the last
**14 days** (`TIME_WINDOW_LOOKBACK_DAYS`; the filter runs in SQL) that

- is not archived, was made by a person or their agent (origin `user` or
  `ai_executed`, not a product of review), is not an onboarding checkpoint,
  and is not an instrument-booking note (`booking_*` metadata: it describes a
  reserved window, not a capture made when it was synced),
- carries no session target and no session id or link code in its metadata,
- and whose capture time falls inside exactly one session window of its
  project,

it proposes `note -was_derived_from-> session` with
`basis: time_window_match`, `origin: system_detected`, status `proposed`.
Idempotent: a pair already linked in any status (a declined link included) is
never re-proposed, and a note already linked to *any* session in any status is
left alone, so a declined guess is not replaced by another. The review page
lists it under **Proposed provenance links** as "made during this session".
Accepting records `acceptance_mode=human_selected`; like other note-to-session
links it does not render as `prov:wasDerivedFrom` in PROV-O export, which
carries accepted note-to-note links only.

## Session suggestions

`GET /projects/{project_id}/session-suggestions` (project read access; an
unreadable project is a 404) computes, on every read and without writing
anything, a report:

```json
{
  "project_id": "...",
  "generated_at": "2026-09-28T13:00:00Z",
  "timezone": "UTC",
  "quiet_threshold_minutes": 240,
  "lookback_days": 14,
  "min_captures_per_day": 3,
  "suggestions": [
    {
      "suggestion_id": "close_quiet_session:<session_id>:<last_note_id>",
      "kind": "close_quiet_session",
      "title": "End the operational session LT-... at 11:40 on 2026-09-28",
      "detail": "Still open, but its last capture was 6 h ago (4 captures in all).",
      "session_id": "...", "start_at": null, "end_at": "2026-09-28T11:40:00Z",
      "capture_count": 1, "capture_note_ids": ["..."]
    }
  ]
}
```

| Kind | When | Suggests | Stable id |
| --- | --- | --- | --- |
| `close_quiet_session` | An **active** session whose most recent capture (a note targeting it, or the source of a proposed/accepted link to it) is older than **4 hours** | End it at that capture's time (never before its start) | session + last capture, so new captures raise a fresh suggestion |
| `start_session_from_captures` | A local day with **3 or more** staged captures that name no session and fall in no session window (bookings and onboarding checkpoints excluded) | A session spanning the first to the last of them, listing their note ids | project + local day, so a dismissed day stays dismissed |
| `start_session_from_booking` | A note with `booking_start`/`booking_end` metadata (an instrument-calendar booking) that has begun within the last 14 days and whose window **no session overlaps** | A session for the booking window, listing the booking note and the sessionless captures made in it | project + hash of `booking_uid` (else the note id); a re-synced booking suggests once |

A session with no captures at all is never suggested for closing: there is no
honest time to end it at. Captures inside a suggested booking belong to that
booking's suggestion, not to a day suggestion. Bounds: at most 50 suggestions
per read, 200 listed capture ids per suggestion, and the 500 most recent notes
per active session.

### In the app

A **Session suggestions** card appears on the Review page (`/app/batches`, for
the selected project) and below **Sessions** on the workspace home. It hides
itself when there is nothing to suggest or the read fails.

- **Apply** calls the ordinary session routes as the signed-in person
  (contributor access): `PATCH /sessions/{id}` with `status: closed` and the
  suggested `ended_at`; or `POST /sessions` with `session_type: operational`
  and the suggested `started_at`, followed by `PATCH` to close it when the
  suggested end is already past (an ongoing booking stays open).
- **Apply and attach N captures** does the same, then adds the new session to
  each listed note's targets with `PATCH /notes/{id}`, keeping the note's
  existing targets. Only this click attaches anything.
- **Dismiss** hides the suggestion on this device (`localStorage` key
  `lab-tracker:session-suggestion-dismissed:<suggestion_id>`). Storage that is
  unavailable is tolerated: the suggestion hides for the view and may return
  on reload.

`POST /sessions` accepts an optional `started_at` for a session recorded after
the fact. It must carry a timezone offset and may not be more than five
minutes in the future; omitted means now.

## Day-log grouping in the daily review

After the model's patch validates, the batch path appends one deterministic
`create_note` proposal per session that had at least **3 short bench
captures** in the batch. A short bench capture is a staged note a person made
at the bench, not an adapter's import (no `evidence_source_provider`, not a
booking, not an earlier day log), and one of: text of at most 1000 characters,
an `audio/*` upload with a transcript of at most 1000 characters, or an
`image/*` upload. Its session is its single declared session target, else the
session id its metadata names, else the one session open at its capture time.

The proposed note targets the session, is created `committed` when accepted
(it is a reviewed record, not a new capture to redraft), and reads:

```text
Day log — operational session LT-..., 2026-09-28 (America/New_York)

09:02 — Added 5 ml buffer
09:15 — pH reads 7.2
09:40 — gel-lane-3.png
```

Each line is `HH:MM — <first line of text, transcript, or file name>`, at most
120 characters; at most 200 lines are written and the rest are counted; a log
whose captures span several local days gets a heading per day. The
operation's rationale reads "grouped N captures from <session>. Deterministic
day-log grouping by Lab Tracker, not model output; …", it has no confidence
score, and its `source_refs` cite every capture explicitly. The change set
records each appended log under `context_packet.day_logs` (a server-written
field the model cannot set); the review page labels it **Deterministic
grouping (no model)**, and on commit the note is stamped
`origin_provider=lab_tracker`, `origin_model=deterministic_day_log`,
`origin_prompt_version=day_log/v1` instead of the batch's model. The
individual capture proposals stay in the draft; accept the log, the items, or
both.

It sends nothing to the drafting provider (it is built after the model call
from notes already in the batch, adding no one else's captures). Like any
pending proposal, it can later appear in the same reviewer's review memory as
a label of at most 200 characters. It is idempotent: the draft is keyed by the
batch, and a log whose `day_log_key` (session plus the exact capture set)
already exists as a note is not proposed again. A failure in the stage is
logged and the batch keeps the model's proposals. The day log rides on the
batch draft, so it appears only when the model call succeeds: without a
configured provider there is no draft to append it to.

## Effort, limits, and switches

- Per capture: none. Sessions, time-window links, and day logs come from what
  was already captured.
- Per decision: one click (accept or reject a link, Apply or Dismiss a
  suggestion, accept or reject the day log).
- The constants above (14-day lookback, 4-hour quiet threshold, 3 captures)
  are code constants, not settings.
- There is no separate kill switch. Reject a link and it never comes back;
  dismiss a suggestion on a device; reject a day log. Turning off the daily
  review stops the detector and the day log with it; the suggestion read runs
  only when the card is shown.
