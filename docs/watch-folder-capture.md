# Watch Folder Capture

Lab Tracker can watch folders without becoming a file browser, object store, or
experiment tracker. The generic `lt watch` flow records small local JSON events
first, then `lt watch sync` applies those events to the right Lab Tracker sink.

The first sinks are:

- `staged-note`: upload raw evidence files or compact manifest summaries as
  staged notes for normal human review.
- `acquisition-output`: register observed files as outputs of an existing
  acquisition session so the session can later be promoted into a dataset.
- `acquisition-collection`: fingerprint an entire acquisition folder into one
  immutable collection snapshot. The files are manifest members, not individual
  Lab Tracker records.

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

## High-cardinality Acquisition Collection

Use `acquisition-collection` when one experimental run emits many trial files
that belong together. One scan writes one outbox event, and one sync request
registers a content-addressed snapshot containing the root-relative path,
SHA-256 checksum, and size of every member:

```bash
lt watch scan \
  --mode files \
  --sink acquisition-collection \
  --root D:/rig2/session-001 \
  --session <SESSION_UUID> \
  --collection rig2-session-001 \
  --complete

lt watch sync
```

`--collection` is the stable collection key: 1–120 characters, starting with a
letter or digit and containing only letters, digits, periods, underscores, or
hyphens. `--complete` explicitly seals the snapshot for later Dataset
promotion; omit it while the acquisition is still being assembled. Completion
is never inferred from folder quiet time. A complete collection scan cannot be
combined with a truncating `--limit`.

The local event keeps the canonical manifest and the watched root, but Lab
Tracker does not upload the member bytes. A standalone `lt watch sync`, or a
retry from a later command, rehashes every member and marks the event stale if
anything changed. `lt watch run` reuses the stable fingerprints it just
computed, avoiding a second pass over a large folder in the same process.

Collection sync first checks `/schema/describe` for
`acquisition_collections_v1`. If the server does not advertise it, the event
stays queued with upgrade guidance. The client never falls back to thousands of
`acquisition-output` requests.

For server-first deployment, compatibility, device scope, and recovery, see
[Acquisition Collections: Use and Rollout](acquisition-collections.md).

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
      "sink": "acquisition-collection",
      "session_id": "SESSION_UUID",
      "complete": true
    }
  ]
}
```

For an `acquisition-collection` entry, `name` is required and is used as the
immutable collection key. The equivalent command is:

```bash
lt watch add D:/rig2/session-001 \
  --sink acquisition-collection \
  --session <SESSION_UUID> \
  --name rig2-session \
  --complete
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
  `lt watch scan` again to capture the new checksum.
- `session_id must not be empty`: `acquisition-output` events need
  `--session <SESSION_UUID>` or a configured `session_id`.
- `acquisition-collection scans require --collection`: direct collection scans
  need a stable key; configured collection watches use their required `name`.
- `server does not advertise acquisition_collections_v1`: upgrade the Lab
  Tracker server, then retry `lt watch sync`; the collection event remains in
  the local outbox.
- Sync fails but events remain local: fix connectivity/authentication and rerun
  `lt watch sync`; failed events are retryable.
- Large outputs should not be uploaded as raw files: write a manifest with
  artifact pointers instead of scanning the result directory in `files` mode.
