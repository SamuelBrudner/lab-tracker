# Phone Capture Quickstart

Use this when one computer is running Lab Tracker and a bench phone should
capture notes into the same graph.

## Pair the Phone

1. On the serving computer, open Lab Tracker and sign in.
2. Open `Devices` from the top navigation.
3. Create an enrollment QR code.
4. On the phone, scan the QR code and save the device grant.
5. Open the capture page shown by the QR or helper script.

The phone must be on the same LAN or VPN as the serving computer unless the
instance is hosted at a public HTTPS URL.

## Start a LAN Instance

On macOS or Linux:

```bash
scripts/serve-lan.sh --use-postgres
```

On Windows:

```powershell
.\scripts\serve-lan.ps1 -UsePostgres
```

The macOS/Linux helper prints both the normal app URL and the phone capture URL.
It also prints a terminal QR code when the `segno` Python package is available.
The Windows helper prints the health and app URLs; append `/capture` to the app
URL for phone capture.

## Capture at the Bench

- **Text, photo, or voice.** Type a note, attach a photo, or tap the
  microphone to record a voice note in the page. Browsers without in-page
  recording fall back to the phone's recorder app.
- **Offline is fine.** Text, photo, and voice captures that cannot reach the
  server are queued on the phone and upload when it is back online. Unsent
  text is also kept on the device and offered back if the page reloads.
- **Updates find the phone.** The app checks the server for a new version
  whenever you bring it back to the foreground, and hourly while it stays open.
  When one is ready, a banner offers **Reload to update**; queued captures and
  unsent text survive the reload.
- **Same context as last time.** The question and session from your previous
  capture in a project are preselected for the next one; change them when the
  work moves on.
- **Scan into a session.** On the serving computer, open an active session
  and scan its **Capture into this session** QR code (or tap **Capture** on the
  session). Every capture from that page arrives linked to the session.
- **Tap a tag instead.** The same section writes the session's capture link to
  an NFC sticker (**Write NFC tag** on Chrome for Android, or copy the link into
  any NFC writer app). Tapping the tag opens capture for that session.
- **Share a batch once.** When the share sheet hands Lab Tracker several items,
  **Trust shares into _session_ for 1h / 2h / 4h** imports them, and the next
  ones, straight into that session until the window ends or you tap **Stop**.
- **Import the session's photos.** With a session selected, **Import photos**
  uploads many photos at once as one group, with per-file progress and retry.
- **Debrief.** Closing a session offers a one-button voice debrief (three
  prompts); **Skip** is one tap and never holds the close back.
- **Photograph the label.** If the server has the optional `decode` extra, a
  photo showing a session's `LT-<code>` or its capture QR proposes a link to
  that session for review. A GS1 barcode on a reagent adds its GTIN, lot,
  expiry, serial, and catalog number to the note. This is local barcode
  decoding, not OCR, and it never blocks the upload. See
  [decoded-labels-and-file-headers.md](decoded-labels-and-file-headers.md).

## Bench Shortcuts

- **Kiosk scan station:** open `/app/capture?kiosk=1` on a shared bench PC with
  a USB barcode scanner; every scan becomes a staged note in the chosen session.
- **Hands-free voice:** an iOS Shortcut or Android automation can post a voice
  memo to `POST /notes/voice-capture` with its own paired-device credential,
  created under **Devices → Hands-free shortcut**.
- **Desktop bookmarklet:** **Devices → Desktop bookmarklet** saves the page you
  are reading (title, address, selection) through the capture page.

Setup, what each one records, limits, and kill switches are in
[`docs/bench-capture.md`](bench-capture.md).

## Firewall Checks

- macOS: allow Python or the terminal app through the incoming-connection prompt
  when it appears.
- Linux: allow TCP port `8000` in the local firewall, for example with
  `ufw allow 8000/tcp`.
- Windows: run the firewall command in
  [`docs/lan-shared-graph.md`](lan-shared-graph.md) from an Administrator
  PowerShell if the phone cannot connect.

## Write Access

Viewer accounts can open the app but cannot capture notes. Use `Request edit
access` from the app, or ask an admin to grant the admin global role or a
project contributor/owner membership.

## Optional Automatic Voice Transcription

Voice captures upload and remain reviewable without any AI configuration. The
manual **Transcribe** action is always available. To have the server start
best-effort transcription after each new audio upload, explicitly configure an
OpenAI or Google provider and opt in:

```dotenv
LAB_TRACKER_GRAPH_DRAFT_PROVIDER=openai
LAB_TRACKER_OPENAI_API_KEY=...
LAB_TRACKER_AUTO_TRANSCRIBE_VOICE_CAPTURES=true
```

The same behavior applies to all new audio captures, including tagless phone
captures and offline-queued captures when they eventually upload. A short
capture hint is passed as the provider prompt. Upload success never depends on
the provider: failures leave the note pending for the manual action, and a
human transcript or note edit made while the provider call is running is never
overwritten.

This option is off by default. Enabling it sends the raw recording and capture
hint outside the Lab Tracker instance and may incur a paid call for each new
audio capture. Lab Tracker does not yet enforce a per-person rate limit or
daily transcription budget; exact upload replays are deduplicated, but public
or otherwise unbounded deployments should leave the option disabled.
