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
