# Server Capture Channels

Some research activity happens where no Lab Tracker client runs: a quick
thought typed into Slack, a result mailed from a phone, a booking on the shared
confocal's calendar, a `.fcs` file dropped onto the lab OneDrive from the core
facility PC (gap G7 in
[`experiment-walkthrough-coverage.md`](experiment-walkthrough-coverage.md)).
The server capture channels observe those places from the Lab Tracker server
itself, so the per-event effort for the scientist is zero or one gesture.

The same rules hold for every channel:

- **Staged notes only.** Every capture lands as a staged note with pointers and
  bounded text. Nothing is committed, no proposal is accepted, and no target is
  linked that a person did not declare. Drafting and review stay the normal
  human-gated daily review.
- **Honest attribution.** A person who acted — saved a Slack message, sent a
  verified email — authors their capture (`created_by` is their user id; the
  channel acts as a non-interactive principal, so the human-commit gate can
  never admit it). A record the server fetched on its own — a calendar
  booking, a new file in a registered store — is authored by the `SYSTEM`
  principal, never by the booking's organizer or the file's owner. Every note
  carries `capture_channel` (`slack`, `email`, `ics`, or `store_scan`).
- **Opt-in by the operator.** Nothing runs until configured, an unconfigured
  poller makes no network call, and every setting is validated at startup
  (a malformed or half-configured channel refuses to start, naming the
  variable). See [`configuration.md`](configuration.md#server-capture-channels).
- **Pointer, not reimplementation.** Bytes stay where they are. Stored text is
  bounded (8,000 characters for Slack and email bodies, 2,000 for a booking),
  and only small email attachments of a few types are stored at all.

| Channel | Triggered by | Author | `capture_channel` | Effort per event |
| --- | --- | --- | --- | --- |
| Slack slash command / message shortcut | the person, in Slack | the mapped person | `slack` | one command or menu click |
| Email to a capture address | the person's email | the verified sender | `email` | one email |
| Instrument calendar (ICS) | the poller | `SYSTEM` | `ics` | none |
| Registered-store scan | the poller | `SYSTEM` | `store_scan` | none |

System-authored notes (bookings, store files) have no author user, so no
person's per-author scheduled batch or **Run now** includes them. They appear
in the staged inbox and in the coverage read (`GET /projects/{id}/coverage`)
under their `evidence_source_provider` (`ics`, `data_store`), where a person
can request a note-scoped draft for one or set it aside with a reason. Routing
system-fetched captures into a named reviewer's batch is not built yet.

## Slack

Two inbound endpoints; no bot token is used and nothing calls Slack back.

- `POST /integrations/slack/commands` — a slash command such as
  `/lt Rig 2 fly 12 looks dehydrated`.
- `POST /integrations/slack/interactivity` — a message shortcut, "Save to Lab
  Tracker", on any message's ⋮ menu.

Both are authenticated by Slack's request signature, not a bearer token: the
server recomputes `v0=HMAC-SHA256(signing secret, "v0:<timestamp>:<raw body>")`
over the exact raw body, compares it in constant time, and rejects any request
whose `X-Slack-Request-Timestamp` is more than five minutes from the server's
clock (replay window) or whose body exceeds 64 KiB. A failed check answers
`401`; with no signing secret configured both paths answer `404`.

A verified request is then mapped:

1. The channel id must be in `LAB_TRACKER_SLACK_CHANNEL_PROJECTS`.
2. The acting Slack user id must be in `LAB_TRACKER_SLACK_USERS`, mapped to a
   Lab Tracker user id, username, or an address in
   `LAB_TRACKER_CAPTURE_USER_EMAILS`.
3. That user must be a contributor on the mapped project.

If any step fails, the person gets an ephemeral reply saying why and nothing is
stored. Otherwise the text becomes a staged note in the mapped project,
authored by the mapped user, with `origin_provider=slack` and metadata
`capture_channel=slack`, `slack_team_id`, `slack_channel_id`, `slack_user_id`
(who saved it), `slack_capture_kind`, and for a saved message
`slack_message_ts`, `slack_message_user_id` (who wrote it), `slack_permalink`
(`https://<workspace>.slack.com/archives/<channel>/p<ts without the dot>`, when
`LAB_TRACKER_SLACK_WORKSPACE_URL` is set), and `captured_at` (when the message
was posted). The note's `client_capture_id` is derived from the team, channel,
message timestamp, and saver, so a retry or a second click is idempotent while
two people saving the same message each record their own act. Slash commands
are keyed by Slack's per-invocation `trigger_id`. The request finishes with one
database write, well inside Slack's three-second budget.

Slack renders the ephemeral reply for the slash command. For the message
shortcut Slack may not display a reply body; the capture still lands, and an
unmapped user or channel still stores nothing.

### Slack app manifest

Create an app at <https://api.slack.com/apps> "From an app manifest", replacing
the host:

```yaml
display_information:
  name: Lab Tracker
features:
  bot_user:
    display_name: Lab Tracker
    always_online: false
  slash_commands:
    - command: /lt
      url: https://lab-tracker.example.org/integrations/slack/commands
      description: Save a note to Lab Tracker
      usage_hint: "[what you observed]"
      should_escape: false
  shortcuts:
    - name: Save to Lab Tracker
      type: message
      callback_id: save_to_lab_tracker
      description: Save this message as a staged Lab Tracker note
oauth_config:
  scopes:
    bot:
      - commands
settings:
  interactivity:
    is_enabled: true
    request_url: https://lab-tracker.example.org/integrations/slack/interactivity
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```

Install it to the workspace, copy **Basic Information → Signing Secret** into
`LAB_TRACKER_SLACK_SIGNING_SECRET`, and map channels and people:

```bash
LAB_TRACKER_SLACK_SIGNING_SECRET=<signing secret>
LAB_TRACKER_SLACK_WORKSPACE_URL=https://mylab.slack.com
LAB_TRACKER_SLACK_CHANNEL_PROJECTS='{"C0123ABCD": "<project uuid>"}'
LAB_TRACKER_SLACK_USERS='{"U0456EFGH": "alice"}'
```

The server must be reachable from Slack over HTTPS. The install produces a bot
token Lab Tracker never uses; keep it out of Lab Tracker's configuration.

Kill switch: unset `LAB_TRACKER_SLACK_SIGNING_SECRET` (both endpoints then
answer `404`), or remove a channel or person from the maps.

## Email to capture

A poller reads a dedicated IMAP mailbox (implicit TLS, certificate verified)
and stages each accepted message.

### Address scheme

Each person gets one private address per project:

```
<local>+<token>@<domain>        e.g. capture+k3m7…q2x@lab.example.org
```

`<local>@<domain>` is `LAB_TRACKER_EMAIL_CAPTURE_ADDRESS`, and the mailbox must
receive plus-addressed mail for it (most providers deliver
`capture+anything@` to `capture@`). The token is the first 20 base32
characters (100 bits) of an HMAC-SHA256 of `user_id|project_id` under a key
derived from `LAB_TRACKER_AUTH_SECRET_KEY`. Nothing is stored: the server
recomputes the token for the sender's projects and compares in constant time.
Rotating the auth secret therefore changes every capture address. A signed-in
contributor sees their address under **Devices → Email capture** in the app
(for the project selected there), which reads it from
`GET /projects/{project_id}/capture-address`
(interactive sessions only — personal access tokens are refused, because the
address is a capability). The endpoint also returns the sender addresses
accepted for them.

### Threat model

Sender spoofing is the main risk: anyone can put any address in `From`. A
message is accepted only when **both** hold:

1. it is addressed (in `To`, `Cc`, `Delivered-To`, `X-Original-To`, or
   `Envelope-To`) to a capture address whose token matches the (user, project)
   pair, and
2. its single `From` address is one the operator mapped to that same user in
   `LAB_TRACKER_CAPTURE_USER_EMAILS`, and that user can contribute to the
   project.

So a stranger needs a person's secret capture address *and* must spoof that
person's exact address; a colleague's mail to someone else's capture address
fails the token check even though the colleague is a known sender. What
remains: someone who learns a capture address can spoof its owner's `From`.
The damage is bounded — a staged note in that person's inbox that they can set
aside, never a committed record — and it is further reduced by having the
mailbox provider reject mail that fails DMARC/SPF alignment for your domain,
and by treating the address like a password (it can be rotated only by rotating
the auth secret). Messages with no `From`, more than one `From` header or
address, a `From` header with parse defects (e.g. `alice@lab.org
<mallory@evil.com>`, which lenient parsers read as `alice@lab.org` while DMARC
aligns on `evil.com`), an unknown sender, no capture address, a token mismatch,
or a sender who cannot write the project are rejected: they are marked
processed and counted in the poll report (`rejected_<reason>`), and nothing is
stored. Only the headers of a message are interpreted until the sender and the
token have both been verified; the body and attachments of a rejected message
are never read.

### What is captured

- One staged text note: the subject as the first line and `evidence_title`,
  then the plain-text body (HTML-only mail is converted crudely), with a
  trailing quoted reply stripped conservatively — only at an explicit
  `-----Original Message-----`, an Outlook underscore rule followed by
  `From:`, or an `On … wrote:` line after which every line is `>`-quoted.
  Interleaved replies are kept whole. At most the first 64 KiB of the text
  part is scanned (in linear time), and the stored body is bounded to 8,000
  characters.
- Attachments that are PNG, JPEG, GIF, WebP, TIFF, HEIC/HEIF, PDF, or CSV and at
  most 10 MiB (and at most `LAB_TRACKER_MAX_UPLOAD_BYTES`), up to ten per
  message, each become a separate staged file note sharing the text note's
  `capture_bundle_id` (the existing photo+voice bundle mechanism, so drafting
  sees them together), with `evidence_content_hash` set to the file's SHA-256.
- Every other attachment is recorded in the text note by name, content type,
  size, and SHA-256 only (up to twenty), never stored.
- Metadata: `capture_channel=email`, `email_from`, `email_message_id`,
  `captured_at` (the `Date` header), `evidence_source_provider=email`.

The notes' `client_capture_id` derives from the `Message-ID` (or the message's
SHA-256 when absent), so re-reading a message stages nothing twice. A message
is flagged `\Seen` — and moved to `LAB_TRACKER_EMAIL_CAPTURE_PROCESSED_FOLDER`
when set — only after its notes are stored or it was deliberately rejected; a
storage or IMAP failure leaves it unseen for the next poll. The poller fetches
with `BODY.PEEK[]`, reads at most 25 unseen messages per poll, and skips (and
marks) messages over 25 MiB. Use a dedicated mailbox: a person reading it in a
mail client marks messages seen and hides them from the poller.

Kill switch: unset `LAB_TRACKER_EMAIL_CAPTURE_ADDRESS`, or remove a sender from
`LAB_TRACKER_CAPTURE_USER_EMAILS`.

## Instrument bookings (ICS)

Configure one entry per instrument calendar feed:

```bash
LAB_TRACKER_BOOKING_CALENDARS='[{"project_id": "<uuid>", "instrument": "Confocal 1",
  "url": "https://booking.example.org/confocal1.ics?token=…",
  "timezone": "America/New_York"}]'
```

Feeds must be HTTPS. They are fetched through the same outbound HTTP egress
policy as artifact resolution (private and loopback addresses are refused
unless `LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES` and
`…_ALLOWED_NETWORKS` admit them), every redirect is re-authorized and may not
downgrade to HTTP, and a fetch is capped at 2 MiB and 20 seconds. Feed URLs
often embed a secret token, so they are never logged, stored in a note, or
echoed in a report.

The parser is a small RFC 5545 subset: line unfolding, `VEVENT`s (nested
`VALARM`s ignored), `UID`, `SUMMARY` (unescaped), `ORGANIZER` (`mailto:` only),
`STATUS`, `RECURRENCE-ID`, and `DTSTART`/`DTEND`/`DURATION` as UTC, as
`TZID`-qualified local times resolved through `zoneinfo` (common Windows and
`/vendor/…/Area/City` spellings are mapped), as floating times in the feed's
`timezone`, or as all-day dates. It does **not** expand `RRULE`/`RDATE`: a
recurring series yields only its own first instance plus any overridden
instances, so feeds should list bookings as individual events (booking systems
typically do). Unknown `TZID`s skip that event.

Each booking whose start lies in `[now − 1 day, now + 7 days]` is upserted as
one staged note per (UID, instance start — the `RECURRENCE-ID` when present),
authored by `SYSTEM` with `origin_provider=ics` and metadata:

| Key | Meaning |
| --- | --- |
| `booking_uid` | the event `UID` |
| `booking_start`, `booking_end` | ISO-8601 UTC (`+00:00`) |
| `booking_instrument` | the configured instrument name |
| `booking_summary` | the event summary (bounded) |
| `booking_organizer` | the organizer's email, only when given as `mailto:` |
| `booking_all_day`, `booking_status`, `booking_recurrence_id` | when present |
| `evidence_capture_kind` | `instrument_booking` |

While the note stays staged, later polls keep its `booking_*` metadata in step
with the feed (a moved or cancelled booking updates it and stamps
`booking_updated_at`); the note's text records the booking as first seen. A
cancelled booking that was never captured is not staged. Once a person has
reviewed the note (it is no longer staged), the feed never rewrites it. At most
200 bookings per feed are handled per poll; bookings deleted from the feed are
not detected.

Kill switch: remove the feed from `LAB_TRACKER_BOOKING_CALENDARS`.

## Registered-store scans

For a store registered with `POST /data-stores`, a scan stages one pointer
note per new file under a prefix:

```bash
LAB_TRACKER_STORE_SCANS='[{"project_id": "<uuid>", "store": "lab-onedrive",
  "prefix": "experiments/001/flow", "patterns": ["*.fcs", "*.csv"]}]'
```

`patterns` are case-insensitive globs matched against the file name or its
path below the prefix (default `["*"]`); they are applied while listing, so
only matching files count toward the listing cap. Dot files, Office lock files (`~$…`),
and `*.tmp`/`*.part`/`*.partial`/`*.crdownload` are always ignored. The store
name resolves like `store://` resolution: the project's own store first, then
its group's. A project store with that name always wins, even when it is
refused; a scan never falls back to the group's store.

Every scan run is held to the store's operator authority grant
([`LAB_TRACKER_STORE_AUTHORITY_GRANTS_JSON`](configuration.md#scoped-store-authority-grants)),
exactly as resolution and health are. The poller detaches the store's persisted
grant binding while its database read is open, releases that read, and
revalidates the binding against the worker's startup registry snapshot before
it builds a listing adapter or starts any filesystem, credential, or subprocess
work. Listing requires the grant's `list` capability; streaming a file for its
SHA-256 additionally requires `bytes_by_path` (without it the scan still lists
and stages each file with `content_hash_pending=true`). For a store kind that
can be listed, a store registered
before grant bindings existed, a revoked or changed grant, a grant for another
scope, or a grant without `list` fails that scan with the static detail `The
data store is not authorized for listing by a current operator grant.` and no
I/O; it never says which check failed or names the store's root, remote, or
grant. Registrations are immutable and cannot yet be rebound, so to scan a
location registered before grants existed, register it again under a grant
(with a new name) and point the scan at that registration. That is a new scan
with its own baseline; see the upgrade note below before re-pointing it.

- **rclone-backed kinds** (`s3`, `gcs`, `azure_blob`, `dropbox`, `gdrive`,
  `box`, `onedrive`, `ssh`, `rclone`) — one bounded `rclone lsjson --recursive
  --files-only` through the shared process executor (8 MiB output cap, the
  resolver subprocess deadline). The remote and root come only from the
  revalidated grant binding, and the exact `LAB_TRACKER_RCLONE_ALLOWED_REMOTES`
  policy still applies as an outer ceiling. `--hash` is requested only for
  backends that store a hash (`s3`, `gcs`, `azure_blob`, `dropbox`, `gdrive`,
  `box`, `onedrive`) so an SFTP or generic remote is never asked to re-read
  every file; SHA-256 is computed by streaming `rclone cat` under the same cap.
- **`local_fs`** — not supported in this build. After the grant is
  revalidated, the scan fails with the static detail `Local store scans are not
  supported in this build.`, before it enumerates, stats, or hashes anything.
  The local listing adapter checks only the global
  `LAB_TRACKER_RESOLVER_ALLOWED_ROOTS`, not the store's own grant boundary, so
  an alias inside one project's store could reach another project's files under
  a broad root. Local scans stay disabled until the local-use slice retains the
  revalidated grant root inside enumeration and hashing; local resolution and
  health are disabled for the same reason. To capture from a facility PC now,
  sync its folder to an rclone-backed store.
- **`http`, `git`, `object_table`, `database`** — cannot be listed; the scan
  reports `Listing is not supported for <kind> stores.` whether or not the
  store has a grant binding, before any grant check.

Each new file becomes a staged note authored by `SYSTEM` with
`origin_provider=store_scan` and metadata `capture_channel=store_scan`,
`evidence_source_uri` (`store://<name>/<path>`), `store_file_path`,
`store_file_size_bytes`, `store_file_modified_at`, `captured_at` (the file's
modification time, so sessions and day windows place the file by when it was
written rather than when the poll noticed it),
`store_file_provider_hash_<algorithm>` (when the listing reports one), and
either `evidence_content_hash` (the SHA-256, which feeds the content-hash
provenance detector) or `content_hash_pending=true` when the file is larger
than `LAB_TRACKER_STORE_SCAN_HASH_MAX_BYTES`, unreadable, changed while being
read, or past the scan's five-minute hashing budget. A SHA-256 is never
guessed. The note never carries the bytes.

"New" is defined carefully:

- The **first run** of a scan records what is already there as a baseline (in
  the poll state file) and stages nothing, so enabling a scan on a full store
  does not flood the review inbox. Set `"include_existing": true` to stage the
  existing files instead (at most 100 per poll). `include_existing` matters
  only on a run that finds no baseline: it records an empty baseline, so every
  matching file is staged over that and later polls (each file once), and
  turning the flag off afterward does not restore a baseline. Baselines belong
  to one exact store registration: when the scan's store name starts resolving
  to a different registration (for example, a project store that now shadows
  the group's), the next run records a fresh baseline (apart from the one-time
  upgrade adoption below). A refused scan records no baseline and stages
  nothing.
- A file is identified by store, path, size, and modification time
  (`client_capture_id`), so a file that is rewritten becomes a new capture.
- A file modified within the last two minutes is still settling and waits for
  a later poll, so a growing acquisition is captured once, finished.
- At most 100 new files are staged per scan per poll; the rest follow.

If the poll state file is lost, the next run records a fresh baseline, and a
file that arrived in between is not staged.

Upgrading to a build that keys baselines by store registration opens no capture
gap for a scan whose store already has a grant binding. Earlier builds keyed a
baseline by project, store name, prefix, and patterns only, so the first
authorized poll after the upgrade hands that name-keyed baseline to whichever
registration the name resolves to at that moment, even when that is not the
registration that recorded it (for example, a project store created since then
that now shadows the group's). It does so once: the same write removes the
name-keyed entry, so any later registration under that name starts with a
fresh baseline. A refused poll adopts nothing and leaves the entry in place.

The upgrade is not lossless for a scan whose store was registered before grant
bindings existed. Its scans are refused until the location is registered again
under a grant with a new name, as described above, and pointing the scan at
the new name makes it a different scan: the name-keyed baseline is not adopted
(it stays in the poll state file, unused), and the new scan's first run
records a fresh baseline, so files that arrived while the old scan was refused
are absorbed into it and not staged. To stage them, set `"include_existing":
true` on the re-pointed scan before its first run. That stages every matching
file under the prefix, not only those that arrived in the gap (at most 100 per
poll), and because a file's `client_capture_id` includes the store name,
files already staged under the old name are staged again under the new one.

What remains deferred: `local_fs` scans and local-store health, until the
retained grant boundary reaches the local filesystem helper; native
(non-rclone) S3 versioning; `http`/`git` listing; and `object_table`/`database`
stores, which have no listing adapter.

Kill switch: remove the scan from `LAB_TRACKER_STORE_SCANS`.

## Running the pollers

The email, booking, and store-scan pollers run from the same places batch
dispatch runs:

- **In-process ticker** — set `LAB_TRACKER_INTEGRATIONS_POLLER_ENABLED=true`.
  The app then checks once a minute (idle while no poller is configured).
- **HTTP** — `POST /integrations/run-due` with an admin session, an admin
  `all`-scope token, or the admin `batch_run_due` scheduler token (the same
  credential that calls `/batches/run-due`). It returns a per-poller report.
- **CLI for cron** — on the server host:

  ```bash
  */5 * * * * cd /srv/lab-tracker && lab-tracker integrations poll >> /var/log/lab-tracker-poll.log 2>&1
  ```

  `--only email|bookings|store_scans` (repeatable) narrows the run and
  `--force` ignores the minimum interval for a manual test. The command prints
  a JSON report and exits `1` when a poller failed.

Whoever triggers it, each poller runs at most once per
`LAB_TRACKER_INTEGRATIONS_POLL_MIN_INTERVAL_SECONDS` (default 300); the last
run times live in the poll state file (`LAB_TRACKER_INTEGRATIONS_STATE_PATH`),
shared across processes under a file lock, so the ticker, cron, and the HTTP
trigger never double-poll. A poller that cannot take the lock reports `busy`
instead of running unthrottled. Each report entry has a `status` of `ran`,
`failed`, `skipped_interval` (with `next_eligible_at`), `not_configured`, or
`busy`, plus counts and static error details that never include a feed URL,
mailbox content, or host path.

Pollers are independent: an exception in one poller — or in one feed, message,
or scan inside it — is recorded and the rest carry on, and none of them shares
a code path with the daily-review batch dispatch. Each poller run also has a
four-minute wall-clock budget: once spent, it stops between items (messages,
feeds, scans, files) and reports what it left for its next run
(`left_for_next_poll`, `feeds_left_for_next_poll`, `scans_left_for_next_poll`,
or `deferred`), so one slow poller cannot starve the others.
