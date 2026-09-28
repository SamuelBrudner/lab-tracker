# Watch Folder Capture

Lab Tracker can watch folders without becoming a file browser, object store, or
experiment tracker. The generic `lt watch` flow records small local JSON events
first, then `lt watch sync` applies those events to the right Lab Tracker sink.

The first sinks are:

- `staged-note`: upload raw evidence files or compact manifest summaries as
  staged notes for normal human review.
- `acquisition-output`: register observed files as outputs of an existing
  acquisition session so the session can later be promoted into a dataset.

The boundary is the same as the rest of retained v1: capture facts and pointers
automatically, leave scientific meaning and graph commits to human review.

## Setup

Create a config in the analysis checkout, instrument workstation folder, or synced
folder you want to scan from:

```bash
lt watch init --project <PROJECT_UUID>
```

This writes `.lab-tracker/watch.json` and creates the default outbox at
`.lab-tracker/outbox/watch/`.

Use `LAB_TRACKER_WATCH_CONFIG` when the config lives outside the current
checkout, and `LAB_TRACKER_WATCH_OUTBOX` when the durable outbox should live
somewhere else.

Minimal config:

```json
{
  "version": 1,
  "project_id": "PROJECT_UUID",
  "outbox": ".lab-tracker/outbox/watch",
  "watches": []
}
```

Configured watches are optional. `lt watch scan --root ...` can scan one folder
without adding it to the config.

## Raw Evidence Folder

Use `staged-note` when files in a folder should become staged evidence notes:

```bash
lt watch scan \
  --mode files \
  --sink staged-note \
  --root ./lab-inbox \
  --include "*.md" \
  --include "*.pdf"

lt watch sync
```

Each file event records the absolute file URI, root-relative external ID,
content hash, size, and observed mtime. Sync refuses to upload a file if it
changed after the scan; rescan the folder to capture the new version.

`lt import-folder` remains supported for one-shot folder import. It now shares
the same file discovery rules as `lt watch`: symlinked files are skipped, hidden
paths are ignored, and include/exclude globs are matched against both the
relative path and filename.

## Acquisition Session Output Folder

Use `acquisition-output` when an instrument writes files that should be
registered against an existing session:

```bash
lt watch scan \
  --mode files \
  --sink acquisition-output \
  --root D:/rig2/session-001 \
  --session <SESSION_UUID>

lt watch sync
```

Sync calls the existing session-output API with the root-relative path, SHA-256
checksum, and size. It does not create notes, commit datasets, or choose a
question. Dataset promotion still happens through the retained session workflow.

## Manifest-Producing Workflows

Use `manifest` mode when a workflow can write a compact JSON summary alongside
its outputs. The default manifest filename is `lab-tracker-evidence.json`.

Example manifest:

```json
{
  "capture_id": "run-001",
  "capture_kind": "analysis_evidence",
  "sink": "staged-note",
  "summary": "Decoded stimulus identity from held-out trials.",
  "metrics": {
    "heldout_accuracy": 0.91
  },
  "artifacts": [
    {
      "uri": "file:///scratch/snb6/run-001/summary.png",
      "kind": "figure",
      "title": "Held-out decoding summary",
      "summary": "Accuracy by stimulus condition."
    }
  ]
}
```

Scan and sync:

```bash
lt watch scan --mode manifest --root /scratch/snb6/project-runs
lt watch sync --request-draft
```

`--request-draft` only applies to `staged-note` events. It asks the existing
analysis graph draft endpoint to propose human-reviewed graph changes for the
staged note; it never commits analyses, claims, visualizations, or question
links.

The question, session, and dataset ids declared for a scan (flags, a
configured watch entry, or a manifest) are attached to the staged note as
targets and labelled `declared_target_source=explicit`; `lt watch` has no
default question, so every declared id is a per-capture choice. A stale id
fails the sync loudly instead of landing as metadata only.

## Sessions From Folder Names

A capture that already names its session needs no linking in review. The
watcher attaches a session to a file in three ways, in this order:

1. `lt watch add --session <uuid-or-link-code>` on the watch entry.
2. An `LT-`-prefixed session link code in the watched root's name or in the
   file's path under it. The app shows and copies each session's link code as
   `LT-<code>` (the code itself is 26 characters), so naming an acquisition
   folder `session001_LT-<code>` claims everything saved inside it. Only the
   `LT-` form counts, and only a code exactly as the server prints it: any 26
   letters decode to some id, so
   an unprefixed long folder name never claims a session.
3. The checkout's active session, set with `lt session use <uuid-or-link-code>`
   (or the `LAB_TRACKER_SESSION_ID` environment variable). `lt session use`
   looks the session up on the server and records its project; it fails
   (exit status 1) when the session does not exist or the server cannot be
   reached, so a typo is never recorded. The checkout session targets only
   captures filed into that project; any other capture keeps the id as plain
   metadata (`watch_session_id` or `capture_session_id`). A context written
   by an older client without its project targets nothing and prints a hint
   to rerun `lt session use`. It expires after twelve hours by default
   (`--hours`), so a stale session never keeps claiming next week's
   captures. `lt session status` shows it and `lt session clear` ends it
   early.

The resolved session becomes a note target on the staged note, and the
`watch_session_source` metadata (`config`, `path`, or `active`) says which
rule matched. The first two rules are per-capture choices and keep
`declared_target_source=explicit`; a session taken from the checkout context
is labelled `config_default`, the same weaker label a tool-wide default
question gets, so a reviewer can tell a named folder from a lingering
context. Figure saves made from the same checkout carry the active session
the same way.

## Offline Figure Queue

Figure saves that cannot reach the server (`lab_tracker_client.savefig`,
`capture_figures`, the autotrack hook, `lt capture file`, the R autotrack
hooks, or the MATLAB `labtracker.savefig`) are queued into this same watch
outbox instead of being dropped, under the same capture id and project a live
save would use, and drain with the next `lt watch run`, `lt watch sync`, or `lt outbox
sync` (see [repo-report-capture.md](repo-report-capture.md) for the
all-adapter drain). Set `LAB_TRACKER_CAPTURE_OUTBOX=0` to disable the queue.
A figure that was only displayed inline or shown (never saved) has no file to
point at, so its queued bytes are kept under the outbox's `blobs/` folder, one
file per distinct content; they can be deleted once `lt outbox status` shows
the events synced.

The same outbox holds the daily notebook pages the Jupyter save hook writes
([notebook-and-script-capture.md](notebook-and-script-capture.md)). Each page
carries the reserved `payload.deliver_after` time (its local day's end); a
sync leaves such an event pending, reported as skipped with reason `not_due`,
until that time has passed.

MATLAB writes the event itself (see [lab-tracker-matlab.md](lab-tracker-matlab.md));
its events carry no `mtime`, so the sync checks their content hash and size
alone before uploading.

## Capturing One Saved File From Any Runtime

`lt capture file PATH` runs the same fail-soft capture as `savefig` on a file
that is already on disk, so any runtime or pipeline step can shell out to it:

```bash
lt capture file results/summary.png --metadata stage=final
lt capture file out/fit.pdf --require-bound --metadata capture_language=julia
lt capture file table.csv --kind table --logical-id results/table --project PROJECT_UUID
```

- stdout is the capture result as JSON (`action`, `path`, `client_capture_id`,
  `evidence_content_hash`, `metadata`, `note_id` when stored, `queued_event`
  when queued, `reason`, `errors`) plus `notices`, the stderr lines the
  capture printed, so a caller that discards stderr can still show them;
- the exit status is 0 for every capture outcome (`imported`, `coalesced`,
  `queued`, `skipped`, `failed`) and nonzero only for a usage error, so a
  capture never fails the step that called it;
- `--require-bound` applies autotrack's rule: the file is captured only when
  its project comes from `--project`, `LAB_TRACKER_PROJECT_ID`, or the
  checkout's `lt_ids.json`; otherwise nothing is sent or queued and the result
  is `skipped` with reason `project_unbound`;
- `--metadata KEY=VALUE` (repeatable) adds scalar note metadata; `true`,
  `false`, and numbers that print back unchanged are typed, anything else is
  a string;
- `--output PATH` also writes the result JSON to `PATH` atomically, for a
  caller that runs the command in the background (the R hooks do).

Each invocation is a new process, so the circuit breaker that spares a
Python session repeated connect timeouts lasts for one file only: with the
server down, each call waits at most one clamped connect timeout (2.5 s)
before queueing.

A figure save goes to the project named by, in order: the `project_id`
argument, `LAB_TRACKER_PROJECT_ID`, the saved file's checkout binding
(`lt_ids.json`), the checkout's watch config, and only then the client's or
login profile's default project. The autotrack hook fires in every directory,
so it captures only when the project comes from one of the first three; any
other save is skipped, and nothing is sent or queued. A stderr notice names
each unbound checkout root (or, outside a repository, the save's directory)
once per process.
`lt setup schedule --request-draft` adds `--request-draft` to the scheduled
run so newly synced captures also ask for a graph draft; on macOS the
schedule is a launchd agent under `~/Library/LaunchAgents`, Windows uses Task
Scheduler, and other systems a managed crontab line.

## Configured Watches

You can edit `.lab-tracker/watch.json` to scan repeatable roots:

```json
{
  "version": 1,
  "project_id": "PROJECT_UUID",
  "outbox": ".lab-tracker/outbox/watch",
  "watches": [
    {
      "name": "analysis-manifests",
      "mode": "manifest",
      "root": "/scratch/snb6/project-runs",
      "pattern": "lab-tracker-evidence.json",
      "sink": "staged-note",
      "tags": ["hpc"]
    },
    {
      "name": "rig2-session",
      "mode": "files",
      "root": "D:/rig2/session-001",
      "sink": "acquisition-output",
      "session_id": "SESSION_UUID"
    }
  ]
}
```

Then run:

```bash
lt watch scan
lt watch status
lt watch sync
```

## HPC Adapter

`lt hpc` is still the recommended interface for Slurm workflows because it
knows how to capture scheduler facts, job IDs, git state, log excerpts, and run
lifecycle events. Its watch-folder mode remains compatible with
`lab-tracker-hpc-run.json`.

Use generic `lt watch` for non-HPC folders and manifest-producing tools. Use
`lt hpc` when the source of the evidence is a scheduler run.

## Troubleshooting

- `Watch config not found`: run `lt watch init`, pass `--config`, or set
  `LAB_TRACKER_WATCH_CONFIG`.
- `watched file changed since scan`: the file was modified before sync. Run
  `lt watch scan` again to capture the new checksum. The event is marked
  `stale`, which is terminal: later syncs skip it without spending `--limit`,
  so pending captures are never starved. The next scan (`lt watch run` scans
  first) captures changed content as a new event, or re-arms the stale event
  as `pending` when the file again matches its original content hash.
- `session_id must not be empty`: `acquisition-output` events need
  `--session <SESSION_UUID>` or a configured `session_id`.
- Sync fails but events remain local: fix connectivity/authentication and rerun
  `lt watch sync`; failed events are retryable.
- Large outputs should not be uploaded as raw files: write a manifest with
  artifact pointers instead of scanning the result directory in `files` mode.
