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
| **Capture-setup tips** | Finds what a reviewer's own captures were missing (no session, no debrief, unread NWB headers) and lets the batch drafter recommend the setup that would have recorded it, for captures it could not interpret | A person follows the steps or ignores the tip; nothing is accepted or committed |

None of the four writes a committed record on its own: the first writes
`PROPOSED` provenance links, the second writes nothing at all, the third
appends one `proposed` operation to a daily-review draft, and the fourth adds
read-only advice to a daily-review draft's `context_packet`. Delegated curation
([delegated-curation.md](delegated-curation.md)) treats the day log like any
other `create_note` proposal (admitted by `full`, never by `organize`).

## The capture clock and the session window

All four read the same two facts
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
- **Whose session it was.** Time only ties a capture to a session its own
  author ran: when both the note's author and the session's author are known
  (their user ids), they must match, so a colleague's open session never
  claims your bench photo, and two people's overlapping sessions are not
  ambiguous for either of them. When either author is unknown (legacy rows,
  auth-disabled installs), any session of the project qualifies. A declared
  session target or a session id in the metadata is never second-guessed by
  this rule.

All window lookups go through one timeline per read or batch run: each
session's window is computed once, sessions that ended before the earliest
capture are skipped, and every capture is placed in a single sweep, so the
work grows with captures plus sessions, not their product.

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
- and whose capture time falls inside exactly one window among its project's
  sessions run by the note's author (either author unknown: any session),

it proposes `note -was_derived_from-> session` with
`basis: time_window_match`, `origin: system_detected`, status `proposed`.
Idempotent: a pair already linked in any status (a declined link included) is
never re-proposed, and a note already linked to *any* session in any status is
left alone, so a declined guess is not replaced by another. The review page
lists it under **Proposed provenance links** as "made during this session".
Accepting records `acceptance_mode=human_selected`; like other note-to-session
links it does not render as `prov:wasDerivedFrom` in PROV-O export, which
carries accepted note-to-note links only. The detector reads only the existing
links whose source is one of its candidate notes.

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
| `start_session_from_captures` | A local day with **3 or more** of one person's staged captures that name no session and fall in no window of a session that person ran (bookings and onboarding checkpoints excluded) | A session spanning the first to the last of them, listing their note ids | project + local day + author, so a dismissed day stays dismissed |
| `start_session_from_booking` | A note with `booking_start`/`booking_end` metadata (an instrument-calendar booking) that has begun within the last 14 days and whose window **no session overlaps** | A session for the booking window, listing the booking note and the sessionless captures made in it | project + hash of `booking_uid` (else the note id); a re-synced booking suggests once |

A session with no captures at all is never suggested for closing: there is no
honest time to end it at. Captures inside a suggested booking belong to that
booking's suggestion, not to a day suggestion.

Capture days are per person and offered to their own author: the reader sees
days made of their own captures (plus captures with no recorded author), and
the captures listed with a booking are the reader's too. Applying records the
session as the reader, and time only ties a capture to its own author's
session, so offering a colleague's day would suggest a session that could
never cover it. Quiet sessions and bookings are project facts and every reader
sees them; any session, whoever ran it, covers a booking.

Bounds: candidate captures from the last 14 days (filtered in SQL), booking
notes synced in the last 90 days (`BOOKING_NOTE_LOOKBACK_DAYS`), only the
provenance links that target an active session, the 500 most recent notes per
active session, at most 50 suggestions per read, and 200 listed capture ids
per suggestion.

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
session id its metadata names, else the one session its own author had open at
its capture time (either author unknown: any session). Only day logs recorded
since the earliest capture in the batch are read for the idempotency check.

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

## Capture-setup tips in the daily review

A daily-review batch draft can end with advice for the person whose captures
it read: a capture setup that would have recorded what some of today's
captures lacked. Lab Tracker finds the gaps from capture metadata and offers
them to the drafter as candidates; the drafter picks the ones whose captures
it could not confidently place or interpret and says in a sentence or two what
they were missing; Lab Tracker attaches its own setup steps. Tips are advice,
not proposals: nothing in them can be accepted, rejected, deferred, or
committed, and they never change the draft. Only batch drafts carry them;
note-scoped and analysis drafts do not. The detection and the recording live
in `src/lab_tracker/services/graph_draft_capture_setup.py`, and the copy (titles,
steps, app pages, commands, guides) in `src/lab_tracker/capture_setup_catalog.py`.

### What Lab Tracker looks for

Only the batch reviewer's own staged captures are examined: the notes whose
`created_by_user_id` is the user the batch is assigned to (else the person who
ran it). A batch whose reviewer has no user id (a legacy assignee string) gets
no candidates, because the cooldown below is per user. Time ties a capture to
a session only when the same person ran it, as everywhere on this page, and
"now" is the end of the batch window, so a retried batch judges time the same
way; a retry rebuilds its packet, though, so sessions or drafts that changed in
between can change what it is offered.

| Kind | Gap | The reviewer's captures it covers | Detected at |
| --- | --- | --- | --- |
| `session_capture_link` | `sessionless_app_captures` | Captures from the app's capture flows (`capture_source` `mobile_capture` or `share_target`: the phone or web capture page, kiosk, NFC, share, photo import), other than shortcut memos, bookmarklet captures, and debriefs, that name no session and were made outside every session window their author ran | 3 |
| `shortcut_session` | `shortcut_no_active_session` | Hands-free shortcut memos (`capture_channel=shortcut`) that asked for the latest session while none was open (`capture_session_resolution=none_active`) and could not be placed in a session | 1 |
| `shortcut_session` | `shortcut_without_session` | Hands-free shortcut memos sent without a session (no `capture_session_resolution`) that could not be placed in a session | 1 |
| `watch_folder_link_code` | `sessionless_watch_files` | Files synced by `lt watch` in files mode (evidence adapter `lt-watch-files` or `lt-watch-acquisition`, the only mode that reads an `LT-` code from a folder name) that name no session and could not be placed in a session | 3 |
| `session_debrief` | `closed_without_debrief` | Short bench captures (as defined for day logs above) placed in a session the reviewer ran, closed, and ended by the end of the batch window, when no note in the batch with `capture_purpose=session_debrief` targets that session | 3 thin captures |
| `nwb_h5py` | `nwb_headers_unread` | Captures whose `format_sniff_error` is `h5py not installed`: NWB files `lt watch` synced where h5py is missing, so their start time, identifier, and subject were not read | 1 |

"Name no session" is the session-suggestion test: a staged capture a person or
their agent made, not an onboarding checkpoint or a booking, with no session
target and no session id or link code in its metadata. "Placed" means a single
declared session, a session id in the metadata, or the one window of the
author's own session that contains the capture time. A thin capture carries at
most 40 characters of its own text (its transcript, else its typed text).

Every other gap gives at most one candidate per batch; the debrief gap gives
one per session, after the others, newest end first, for at most 3 sessions
(`MAX_DEBRIEF_SESSIONS`). A candidate is offered whenever it has captures and
its kind is not cooling down (below); `detected` tells the drafter whether the
count reached Lab Tracker's own threshold (the last column). A candidate lists
at most 20 note ids (`MAX_NOTE_IDS`) in capture order plus the full count, and
carries only server values: its id (the gap, or
`closed_without_debrief:<session id>`), kind, gap, `detected`, note ids,
count, and for a debrief the session id and the session's own label
(`<type> session LT-<code>`). It never carries capture text.

### What the drafter sees and returns

The candidates are stored on the draft as
`context_packet.capture_setup_candidates` (only when there are any) and sent in
their own trusted `<trusted_capture_setup_candidates>` block, outside the
untrusted batch context. Before rendering, the prompt re-checks every
candidate against a whitelist: only the known keys, a kind and gap from the
catalog that belong together, well-formed ids, and a session label that
matches that session's link code; anything else is dropped. The untrusted
batch context escapes `<` and `>` as JSON `\u003c` and `\u003e`, so capture
text can neither close that context nor imitate the trusted block, and the
model still reads it exactly. The prompt describes what each kind covers, but
no step, command, link, or menu.

The batch response schema gains a required `capture_setup_recommendations`
list of `{candidate_id, note_ids, explanation}`. The drafter includes a
candidate only when it could not confidently place or interpret its captures,
cites only the ones it could not, and returns an empty list otherwise.
Questions about specific past captures stay in `clarification_requests`; a
capture can appear in both.

There is no fallback. When the drafter omits the field, returns something
that is not a list, or picks nothing usable, no tip is shown; the draft still
records what was offered and returned, so a drafter that declines every
candidate can be measured. The field is never validated with the patch, so a
malformed pick cannot fail an attempt, cost a retry, or appear in retry
feedback.

### What the draft records

After the model's patch validates, Lab Tracker keeps a pick only when it names
an offered candidate that was not already picked and cites at least one of
that candidate's note ids (other ids are dropped), and keeps at most 6
(`MAX_RECOMMENDATIONS`). The explanation has non-printable characters turned
into spaces and whitespace collapsed, and is cut to 280 characters
(`EXPLANATION_MAX_CHARS`). When it is empty or looks like a link or a command
(`://`, `www.`, a backtick, or `lt` followed by a word), Lab Tracker's own
sentence for the gap replaces it, and the review page labels it a Lab Tracker
check instead of the drafter's words.

The result is `context_packet.capture_setup`, a server-written field the model
cannot set, written whenever candidates were offered or the drafter returned
anything:

```json
{
  "version": "capture_setup/v1",
  "offered": ["shortcut_no_active_session"],
  "returned": 1,
  "dropped": 0,
  "recommendations": [
    {
      "recommendation_id": "shortcut_no_active_session",
      "kind": "shortcut_session",
      "gap": "shortcut_no_active_session",
      "detected": true,
      "note_ids": ["..."],
      "note_count": 1,
      "session_id": null,
      "session_label": null,
      "explanation": "The memo reached no session, so ...",
      "explanation_source": "model",
      "guide": {
        "title": "Have a session open when you dictate",
        "steps": ["..."],
        "app_path": "/app",
        "command": null,
        "doc": "docs/bench-capture.md#hands-free-voice-shortcut"
      }
    }
  ]
}
```

`note_ids` are the cited ones, in candidate order; `explanation_source` is
`model` or `server`; `guide` is the catalog's copy at generation time (a debrief
tip's `app_path` names its session), so a later wording fix reaches only new
drafts. Tips ride on a successful generation, like day logs: without a
configured provider there is no draft, and a draft that fails before its tips
are recorded has none, though it keeps its stored candidates. A failure in either stage (finding candidates
or recording tips) is logged and the batch goes on without tips.

### The cooldown

A kind recommended on one of the reviewer's batch drafts in the project (a
draft assigned to them, or theirs and unassigned) created within 7 days
(`COOLDOWN_DAYS`) of the batch window's end is not offered again. It is per
kind, not per gap: a shortcut tip of either gap holds back both, and one
session's debrief tip holds back the others. Only recommended kinds count; a
candidate the drafter passed over is offered again next time. The cooldown
reads the rows review memory already loads (the project's 50 newest drafts
that are ready, submitted, changes-requested, rejected, or committed), so in a
busy project a tip can come back sooner. There is no dismissal: the cooldown
is what keeps a tip from repeating.

### In the app

On the review page (`/app/batches/{id}` or `/app/graph-drafts/{id}`), a
**Help future captures** block follows **The model wasn't sure about**, in
both the Proposals and Narrative views. Each tip shows its title (and, for a
debrief, the session's label), "Drafter:" or "Lab Tracker check:" with the
explanation, "Based on N captures" with buttons to the first 5, the numbered
steps, the command when there is one, a button to the page it names (**Open
Home**, **Open Devices**, or **Open the session**), and its guide's path. The
block has no accept, reject, defer, or dismiss: it is outside the keyboard
review loop, the kept tally, **Accept all**, commit, and the read-aloud
review. The Daily review queue and the review-ready email do not show tips.
`lab_tracker_get_graph_draft` returns them with the draft, and agents are told
to report them and never run their steps.

### What the catalog leaves out, and why

- **Printing.** There is no print or label feature. A session's capture QR is
  shown on screen, one per session, on the session page; the tip says to scan
  it.
- **A debrief setting.** There is nothing to turn on: closing a session on the
  session page or the Home session list offers the debrief. The tip says to
  record it instead of choosing "Skip", and that the drafter reads the
  recording only through its transcript, so a person should add one before the
  next daily review.
- **Automatic transcription.** It is an operator opt-in that sends audio to an
  external provider, not a setup a person can choose for themselves.
- **`lt project bind`.** Autotracked figures from a checkout bound to no
  project are never captured, so no batch can show the gap; `lt setup status`
  already suggests binding.
- **Agent-session hooks.** They are opt-in, and `lt setup status` deliberately
  never suggests them.
- **Operator channels** (Slack, email capture, instrument calendars,
  registered-store scans). An operator sets them up, and their captures carry
  no per-capture gap a batch could show.
- **Photo QR and barcode decoding.** The batch drafter receives text only, no
  image pixels, so it cannot judge whether a photo needed decoding.
- **A watch whose files took their session from an active-session default**
  (`lt session use` or `LAB_TRACKER_SESSION_ID`). Those files already name a
  session, and the watch metadata does not record which of the two set it,
  so Lab Tracker cannot tell a lingering `lt session use` default from a
  deliberate pin. Manifest-mode watches take their session from the manifest.
- **Code-side captures** (figures, notebooks, runs, pipelines). Analysis work
  is expected to be sessionless, so a session tip would be noise.

## Effort, limits, and switches

- Per capture: none. Sessions, time-window links, day logs, and capture-setup
  tips come from what was already captured.
- Per decision: one click (accept or reject a link, Apply or Dismiss a
  suggestion, accept or reject the day log). A capture-setup tip asks for no
  decision; acting on it is up to the person.
- The constants above (14-day lookback, 4-hour quiet threshold, 3 captures)
  are code constants, not settings. So are the capture-setup bounds in
  `src/lab_tracker/capture_setup_catalog.py`: at most 8 candidates per batch
  (`MAX_CANDIDATES`), 3 of them debrief sessions (`MAX_DEBRIEF_SESSIONS`), 20
  note ids per candidate (`MAX_NOTE_IDS`), 6 tips per draft
  (`MAX_RECOMMENDATIONS`), 280 characters per explanation
  (`EXPLANATION_MAX_CHARS`), 40 characters for a thin capture
  (`THIN_CAPTURE_MAX_CHARS`), and a 7-day cooldown per kind (`COOLDOWN_DAYS`);
  the detection thresholds (3 or 1) are `DETECTION_THRESHOLDS` in
  `src/lab_tracker/services/graph_draft_capture_setup.py`.
- There is no separate kill switch. Reject a link and it never comes back;
  dismiss a suggestion on a device; reject a day log. Turning off the daily
  review's schedule stops its scheduled runs of the detector, the day log, and
  capture-setup tips; a batch started with Run now (or the
  `lab_tracker_run_graph_draft_batch` MCP tool) still runs all three. The
  suggestion read runs only when the card is shown. A capture-setup tip cannot
  be dismissed; its kind is not offered again for 7 days.
