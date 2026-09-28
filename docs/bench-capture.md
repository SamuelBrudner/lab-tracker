# Bench Capture

At the bench, hands are gloved and eyes are on the work. These paths cut the
taps per capture on top of the phone capture page
([phone-capture-quickstart.md](phone-capture-quickstart.md)): a barcode kiosk,
NFC station tags, a trusted share window, end-of-session photo import, a voice
debrief, a hands-free phone shortcut, and a desktop bookmarklet.

Every one of them lands a **staged note** through the ordinary capture routes
(`POST /notes`, `/notes/upload-file`, or the raw-body `/notes/voice-capture`).
Nothing is committed, drafted, or linked beyond what the person chose: a session
target is only ever the session they picked, opened, or declared. Captures made
with a paired-device credential are stamped server-side with
`capture_device_token_id` and `capture_device_label` as usual.

| Path | Setup (once) | Effort per capture | `capture_channel` |
| --- | --- | --- | --- |
| Kiosk scan station | Open `/app/capture?kiosk=1` on the bench PC | Scan (0 taps) | `kiosk` |
| NFC station tag | Write a tag from the session page | Tap the tag, capture as usual | `nfc` |
| Trusted share window | One tap: 1 h / 2 h / 4 h | Share from any app (no confirm) | `share` |
| Photo import | None | Pick all photos once | `import` |
| Voice debrief | None | One record tap (or Skip) | `debrief` |
| Hands-free shortcut | Build the shortcut once | "Hey Siri, lab note" | `shortcut` |
| Bookmarklet | Drag it to the bookmarks bar | Click, check, send | `bookmarklet` |

## Kiosk Scan Station

For a shared bench PC with a USB barcode scanner (scanners type the code and
then Enter).

- **Open** `/app/capture?kiosk=1`. Add `&project_id=<id>&session_id=<id>` to
  preselect; otherwise the last-used project and the session this browser last
  captured into (if still active) are used. Someone signs in once; every scan is
  recorded as that person (or the paired device).
- **Capture:** one large, autofocused input. Enter stages a text note
  immediately (`Bench scan: <code>`) with `capture_channel=kiosk`,
  `bench_scan_value=<code>`, the scan clock as `captured_at`, and the chosen
  session as its target. The input clears and keeps focus for the next scan.
- **Offline:** scans go through the same offline queue as phone captures. The
  running list of the last 20 scans shows each one's time and state (Sending,
  Saved, Queued offline, Synced, Failed with Retry). A retry reuses the scan's
  `client_capture_id`, so it never duplicates a note.
- **What it is not:** a log of who scanned what, when, and where, not an
  inventory. Codes are not looked up or resolved.
- **Limits:** a scan value is capped at 256 characters
  (`bench_scan_truncated=true` marks a cut one).
- **Exit:** the **Exit kiosk** button returns to the ordinary capture page with
  the app navigation (and **Sign out**).

## NFC Station Tags

On an active session's page, **Capture into this session** now also offers an
**NFC station tag**:

- Where the browser has Web NFC (Chrome on Android), **Write NFC tag** writes
  one URL record: the session's capture link plus `capture_channel=nfc`. Hold a
  tag to the phone; **Cancel** stops waiting.
- Anywhere else, the same link is shown with **Copy link**. Write it as a URL
  record with any NFC writer app (for example NFC Tools: Write, Add a record,
  URL, paste, Write).
- Tapping the tag with a paired phone opens `/app/capture` with the project and
  session preselected; captures from that page carry `capture_channel=nfc`.
- The tag holds a link, never a credential. It names one session: once that
  session closes it is no longer preselected, so rewrite the tag for the next
  session. The capture page accepts only `nfc` as a channel from a URL.

## Trusted Share Window

The OS share sheet parks shared items in the share inbox, and each batch
normally waits for an explicit **Import** (any web page can post to the share
target). When a session is selected on the capture page, the review also offers
**Trust shares into _session_ for 1h / 2h / 4h**:

- While the window is open, shares go straight into that session
  (`capture_channel=share`, the session as target) without the confirm step,
  including the items on screen when it was opened.
- A banner shows the time left and a **Stop** button.
- The window is per device (browser `localStorage`, every access guarded; a
  browser that refuses storage simply never trusts), per project and session,
  and bound to the person who opened it. It expires on its own, never applies in
  another project or to a session that is no longer active, and is removed at
  sign-out or when another person signs in on the browser.
- A trusted import that fails is left in the inbox for manual review rather
  than retried in a loop.
- Trade-off: during the window a share posted by another web page would also be
  imported, as a staged note in that session. The window is short, the inbox
  bounds still apply, and the note stays staged for review.

## End-of-Session Photo Import

**Import photos** on the session page (and on the capture page whenever a
session is selected) opens a multi-select image picker. The server takes one
file per upload, so each photo becomes its own staged note sharing one
`capture_bundle_id` (`capture_channel=import`, `capture_import_index`,
`capture_import_total`, `captured_at` = the photo's own file time), followed by
one short summary text note (`capture_import_summary=true`, at most 20 file
names listed). All of them target the session and go through the offline queue.
A progress bar and per-file state are shown; **Retry** (per file, or **Retry
failed**) reuses each file's `client_capture_id`.

## Voice Debrief

Closing a session (on the session page or the Home session list) offers a
debrief after the session is already closed; the **Debrief** button offers it
any time. Three prompts (what happened, what surprised you, what would you
change) and one record button: the memo uploads as a staged voice note
targeting the session with `capture_purpose=session_debrief`,
`capture_channel=debrief`, and a capture hint used as the transcription prompt.
**Skip** is one tap, even mid-recording (the recording is discarded), and
nothing here can block or undo the close. Browsers without in-page recording
fall back to the phone's recorder app.

The capture flow does not request drafts at upload time (drafting needs a
transcript and the external-provider acknowledgement, and paired devices may not
request drafts), so the debrief is drafted like any other voice capture: by the
daily review, or from the note.

## Hands-Free Voice Shortcut

A phone shortcut records audio and posts it with no app open.

**Auth.** A phone's device grant is an `ldev_` bearer token the capture page
keeps in the browser's storage; a Shortcut cannot read it, and the Devices page
never shows it. Instead, **Settings → Devices → Hands-free shortcut → Create
shortcut credential** mints a dedicated paired-device credential through the
same enrollment flow a phone uses and shows it **once**. It is listed with the
paired devices, stamped on every capture as `capture_device_label`, and revoked
the same way. Like a paired phone it can read your projects and stage captures,
nothing else. A personal access token with the `stage_evidence` scope also works.

**Request.** `POST /notes/voice-capture` takes the recording as the raw request
body and everything else as query parameters:

| Part | Value |
| --- | --- |
| `Authorization` header | `Bearer <shortcut credential>` |
| `Content-Type` header | any `audio/*` type (e.g. `audio/mp4` for `.m4a`), or `application/octet-stream` with `filename=` ending in an audio extension |
| `project_id` (required) | the project UUID |
| `session_id` | a session UUID, or `latest`: your most recently started active session in the project (none active: no session) |
| `hint` | optional, up to 500 characters, used as the transcription prompt |
| `captured_at` | optional ISO 8601 time from the phone |
| `filename` | optional display name |
| `client_capture_id` | optional idempotency key; an exact replay returns the same note |

The note is staged with `capture_channel=shortcut`, the phone-capture voice
metadata, and `capture_session_resolution` (`explicit`, `latest_active`, or
`none_active`) when a session was requested. The body is read only after the
caller is authorized and is cut off at the server's upload limit
(`LAB_TRACKER_MAX_UPLOAD_BYTES`, 100 MiB by default) while it streams. The
Devices page panel shows the exact URL for a chosen project.

```bash
curl -X POST \
  -H "Authorization: Bearer $LT_SHORTCUT_TOKEN" \
  -H "Content-Type: audio/mp4" \
  --data-binary @memo.m4a \
  "https://lab.example/notes/voice-capture?project_id=<project-uuid>&session_id=latest"
```

**iPhone (Shortcuts app).** 1. *Record Audio* (start immediately, finish on
tap). 2. *Get Contents of URL*: the URL above, Method POST, headers
`Authorization: Bearer <credential>` and `Content-Type: audio/mp4`, Request Body
*File* set to the recorded audio. 3. Optionally *Show Notification*. Name the
shortcut ("Lab note") to run it with Siri, or add it to the Home Screen or the
Action button. To send the phone's clock, add a `captured_at` query parameter
from *Current Date* formatted as ISO 8601.

**Android (Tasker).** 1. *Media → Record Audio* to a file (Format MPEG4, Codec
AAC). 2. *Media → Record Audio Stop* (for example after a button press or a
fixed wait). 3. *Net → HTTP Request*: Method POST, the URL, headers
`Authorization:Bearer <credential>` and `Content-Type:audio/mp4`, *File To
Send* set to the recording. HTTP Shortcuts and similar apps work the same way
with a raw file body. Menu names differ between app versions.

**Kill switch:** revoke the shortcut's credential on the Devices page.

## Desktop Bookmarklet

**Settings → Devices → Desktop bookmarklet** has a draggable **Save to Lab
Tracker** button. On any page, clicking the bookmark opens `/app/capture` in a
new window with the page's title, address, and selected text prefilled; the
person checks it and presses send, and nothing is saved before that.

- The details travel in the URL fragment, which browsers never send to a server,
  and leave the address bar once read.
- It stores a pointer: title (300 characters), URL (2,000 characters; credentials
  removed, secret-looking query values such as `token`, `key`, `password`, or
  `sig` replaced with `REDACTED`, fragments with `key=value` pairs dropped), and
  at most 2,000 characters of selected text. The note carries
  `capture_channel=bookmarklet`, `share_title`, and `share_url`.
- Some sites' content security policy blocks bookmarklets; copy the address and
  capture it on the capture page instead.

## Metadata Keys

| Key | Written by |
| --- | --- |
| `capture_channel` | every path above (`kiosk`, `nfc`, `share`, `import`, `debrief`, `shortcut`, `bookmarklet`) |
| `bench_scan_value`, `bench_scan_truncated` | kiosk |
| `capture_bundle_id`, `capture_import_index`, `capture_import_total`, `capture_import_summary` | photo import |
| `capture_purpose=session_debrief` | voice debrief |
| `capture_session_resolution` | hands-free shortcut |
| `share_title`, `share_url` | bookmarklet (and OS shares) |
