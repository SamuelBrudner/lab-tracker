# Activity-Gap Nudge Capture — Design (proposed)

_Status: **PROPOSED / for-eval — not yet accepted.** This is a **v2 consumer-side
adapter**: it is not part of the retained-v1 surface
([`retained-v1-surface.md`](retained-v1-surface.md)), which wins any scope
disagreement. It is a natural follow-on to the guided-setup + auto-tracking epic
(`lab-tracker-stkn`, 14/15 shipped;
[`guided-setup-and-auto-tracking-design.md`](guided-setup-and-auto-tracking-design.md)),
whose product goal is "easy setup first, then as-automatic-as-possible tracking."
The first gate is a scope/design decision (does this belong at all, and only as
v2), not implementation._

## Product goal

The auto-tracking epic made *intentional* capture nearly automatic: every commit
in an enrolled repo queues a snapshot, watched folders queue file/manifest
events, HPC runs queue scheduler facts. The remaining gap is the **dark matter of
research work** — an exploratory Jupyter session, a one-off script, an analysis
that produced a figure but no commit and no watched artifact, a dead-end that is
never written down. That work is real provenance that silently never enters the
record, because every existing adapter fires on a deliberate act (commit, save,
submit) and this work involves no such act.

The goal is to **notice uncaptured work and prompt the human to describe it** —
without becoming a surveillance tool, a provenance source of its own, or a
standing extraction inbox.

## What exists today, and where the gap is

Three consumer-side, offline-first, staged-note capture adapters ship in retained
v1, all built on the same seam (`watch.make_event` → `watch.write_event` into a
local outbox that drains on `lt <mode> sync`):

- `lt watch` — folders and workflow manifests (`docs/watch-folder-capture.md`).
- `lt hpc` — Slurm submit/begin/finish + manifests (`docs/hpc-analysis-capture.md`).
- `lt repo` / `lt git snapshot` — post-commit evidence (`docs/repo-report-capture.md`).

Each is **event-based and intentional**. Retained v1 states the principle
directly for the repo adapter: *"Capture is event-based — Lab Tracker never
clones or continuously monitors repositories."* The uncaptured-work gap is,
by construction, the set of work that produces **no** commit/save/submit event —
so no existing adapter can see it.

## Decision

Add a fourth adapter that emits a **gap-detecting nudge**, not raw process
telemetry:

1. A **local, client-side** sampler observes only a whitelisted set of research
   tools inside configured project roots.
2. It groups activity into contiguous **windows** and, on window close,
   **reconciles each window against the sibling outboxes** already on disk (the
   git/watch/hpc events). A window that an intentional capture already covers is
   **suppressed**.
3. Only the **residual** — a window of research-tool activity under a project
   root with *no* corresponding commit, artifact, or job — emits **one**
   `activity_gap` event.
4. That event is a **prompt to the human**, carrying only coarse, redacted facts
   (tool names, window bounds, a count of files touched under the root). It
   asserts no scientific meaning, requests no graph draft, and never proposes a
   candidate question/claim/tag. The human, at review time, decides whether to
   write a note, attach an artifact, or dismiss it.

Because it emits only the residual, nudge volume is naturally small and **shrinks
as capture habits improve** — the tool argues for its own obsolescence, which is
the correct incentive.

Reused wholesale from the existing adapters: the event envelope
(`watch.make_event`, `src/lab_tracker_client/watch.py:512`), idempotent staging
(`watch.write_event`, `:581`; dedup key `<capture_id>.<sink>.<event_id>`, `:602`),
the offline outbox + `sync` drain, project-id resolution order, and the
`capture_host_metadata()` host block. The design work is entirely in the
**reconciliation** and in one **new server sink**.

### The central tension: continuous monitoring vs. "event-based, no daemon"

A process sampler is inherently a polling loop, which is in direct tension with
two stated invariants:

- retained v1: *"Lab Tracker never … continuously monitors repositories."*
- the auto-tracking design: *"No daemon, no server background machinery:
  recurrence is git hooks, agent session hooks, and OS schedulers."*

We thread this, rather than violate it, on three points:

- **Strictly client-side and local.** The sampler is a consumer-side process on
  the user's own workstation, exactly like `lt watch run`. There is **no
  server-side monitoring** and no server background machinery — the server only
  ever receives a drained, coarse, redacted staged note. This is the same shape
  retained v1 already blesses for the other adapters ("Lab Tracker never clones
  or continuously monitors" is about the *server*; the *client* watch loop is
  already sanctioned).
- **Owned by the OS scheduler, not a lab-tracker daemon.** `lt setup schedule`
  (stkn.13) already established that a recurring, scheduler-launched,
  `--fail-silent` `lt` process is the accepted recurrence mechanism. The
  activity sampler is registered the same way (login item / Task Scheduler /
  launchd), not spawned as a bespoke daemon.
- **Sampling is not evidence.** The in-memory sample buffer is ephemeral and
  never persisted; only the *derived, reconciled, redacted* window summary is
  written to the outbox. Nothing durable is a process log.

This is the one adapter that needs a resident local loop, and that is the sharp
edge to review. See Judged Alternatives for the lower-fidelity cron variant that
avoids even the resident loop.

## Judged alternatives

- **Raw process-telemetry provenance (rejected).** Log which processes ran,
  argv, file I/O, window focus, and treat it as provenance. Rejected: terrible
  signal-to-noise (95% is browser/Slack/daemons), wrong altitude (records *that*
  python ran, not *what question it bore on* — the semantics only the human has),
  low and falsely-precise provenance (fuzzy temporal correlation dressed up as a
  causal chain), and a large privacy/attack surface (argv routinely carries
  secrets). The gap-nudge keeps the one durable idea (catch uncaptured work) and
  discards the firehose.
- **Reuse `staged-note` with `request_draft=false` (MVP) vs. a dedicated
  `activity-nudge` sink (clean).** MVP needs zero server change but mixes nudges
  into the real evidence stream. The dedicated sink routes nudges to a review
  surface and keeps them out of the auto-draft batch. **Recommend:** ship the MVP
  behind the client work, land the dedicated sink before it is on by default.
- **Coarse cron sampling vs. login-launched resident sampler.** A cron firing
  `lt activity sample` every minute needs no resident process (most
  philosophy-aligned) but has coarse fidelity and higher per-fire overhead. A
  login-launched `lt activity run` samples at ~30–60 s with a small resident
  footprint. **Recommend:** support both; default to the scheduler-owned resident
  loop where the OS allows, cron as the fallback. Decide at the scope gate.
- **Nudge-as-live-feed vs. daily digest (choose digest).** A standing live feed
  of nudges is the "extraction inbox" anti-pattern retained v1 defers. A
  **once-daily, dismissible digest** of residual windows is the accepted shape
  (see Scope).

## Scope & relationship to retained v1

This is **out of the retained-v1 surface** and should be treated as a **v2
adapter**, in the same "for-eval" bucket as `stkn.15`. It must not change v1
default runtime behavior.

The sharpest scope line: retained v1 explicitly **defers** *"automatic question
extraction and extraction-inbox workflows … not a standing system-selected
extraction inbox, and nothing commits automatically."* The nudge must stay on the
right side of that line:

- It lands a **pointer note** (a time window + coarse facts), **never** a
  candidate question, claim, entity, or tag. No extraction, no NLP over content.
- It is **coarse-batched** (a daily digest of residual windows), not a live,
  always-refreshing inbox.
- Every nudge is **dismissible** and human-gated; nothing it produces is ever a
  draft source, and it auto-commits nothing.

The Restoration Ledger shape guidance is honored: an on-demand / batched prompt
with per-item accept/dismiss, "not a standing inbox workflow." If the review
concludes this cannot be built without drifting into an extraction inbox, it
should stay deferred.

Evidence-source metadata (`docs/evidence-source-metadata.md`) maps cleanly:
`evidence_source_provider = "workstation-activity"`, `evidence_capture_kind =
"activity_gap"`, `evidence_adapter = "lt-activity-nudge"`.

## Components

### Client

- **`src/lab_tracker_client/activity.py`** — new module, sibling to `hpc.py`.
  Constants `ACTIVITY_ADAPTER = "lt-activity-nudge"`, `ACTIVITY_CAPTURE_KIND =
  "activity_gap"`. Builds the envelope through `watch.make_event(...)` and stages
  it with `watch.write_event(...)` — no new persistence code.
- **CLI group `lt activity {init, run, status, sync, pause}`** — registered next
  to `hpc_parser` (`src/lab_tracker_client/cli.py:761`). `status` is read-only
  (reads the outbox; agent-safe, unprompted). `init` / scheduler enrollment are
  `--dry-run`/`--yes`-gated and **hard-fail non-interactively** without them,
  matching the stkn consent invariant. `sync` reuses the shared drain.
- **Config `.lab-tracker/activity.json`** (created by `lt activity init`),
  parallel to `watch.json`/`hpc.json`:
  ```json
  {
    "version": 1,
    "project_roots": [
      { "root": "C:/Users/snb6/Documents/GitHub/temporal_gradient",
        "project_id": "PROJECT_UUID" }
    ],
    "tools": ["python", "ipython", "jupyter", "Rscript", "matlab"],
    "sample_interval_s": 45,
    "idle_gap_s": 600,
    "min_session_s": 300,
    "digest": "daily",
    "quiet_hours": ["22:00-07:00"],
    "record_basenames": false,
    "outbox": ".lab-tracker/outbox/activity"
  }
  ```
- **Scheduler enrollment** via the existing `lt setup schedule` pattern
  (`schedule.py`) — a login item / Task Scheduler / crontab line running
  `lt activity run --fail-silent`. Surfaced by `lt setup status` and the
  `applied-repos.json` registry, exactly like the other recurring jobs.

### Sampler loop (`lt activity run`)

1. Every `sample_interval_s`, sample running/foreground processes.
2. **Drop at source** anything not on `tools` *and* not cwd-rooted (or with open
   files) under a `project_roots` entry. Non-matching processes are never written
   to the buffer — no browser, no chat, no argv.
3. Accumulate contiguous per-project windows; close a window after `idle_gap_s`
   of no matching activity or shorter than `min_session_s` (discarded).
4. On window close (or the daily digest flush), **reconcile**: scan
   `.lab-tracker/outbox/**` for events whose `observed_at` overlaps the window
   and whose `source.git_repository_path` / resolved project matches. If a
   commit / watched artifact / job covers it → **suppress**. Else → **emit one
   `activity_gap` event**.
5. Idempotency: `capture_id = "activity-<project>-<yyyymmdd>"`,
   `event_id = "gap-<window-start-epoch>-<hash8>"`. `write_event` is a no-op on
   an existing file, so re-runs over the same day never duplicate.

### Emitted envelope

Same schema as the git staged note, but the payload is a prompt, not a claim:

```json
{
  "version": 1,
  "capture_id": "activity-temporal_gradient-20260709",
  "event_id": "gap-20260709T1405-3f9a2c17",
  "capture_kind": "activity_gap",
  "adapter": "lt-activity-nudge",
  "sink": "activity-nudge",
  "observed_at": "2026-07-09T18:52:00+00:00",
  "source": {
    "provider": "workstation-activity",
    "uri": "C:/Users/snb6/Documents/GitHub/temporal_gradient",
    "external_id": "activity://temporal_gradient/20260709T140500-20260709T145200",
    "content_hash": "<sha256 of the coarse window facts>",
    "project_root": "C:/Users/snb6/Documents/GitHub/temporal_gradient",
    "window_start": "2026-07-09T18:05:00+00:00",
    "window_end": "2026-07-09T18:52:00+00:00"
  },
  "context": {
    "project_id": "PROJECT_UUID",
    "question_id": null, "dataset_ids": [], "session_id": null,
    "tags": ["uncaptured-work", "nudge"]
  },
  "metrics": {
    "active_min": 41,
    "tools": { "python": 33, "jupyter": 12 },
    "files_touched_under_root": 6,
    "intentional_captures_in_window": 0
  },
  "log_excerpt": "",
  "payload": {
    "title": "Uncaptured work: temporal_gradient, 2026-07-09 14:05-14:52",
    "summary": "41 active min in python/jupyter under temporal_gradient with no commit, artifact, or job in that window.",
    "status": "needs_human",
    "request_draft": false,
    "body": "<markdown preamble: this is a NUDGE, not evidence. Do NOT draft graph changes from it. Surface it so the human can decide whether to write a note, attach an artifact, or dismiss.>"
  },
  "host": { "capture_platform": "Windows", "capture_host_label": "...", "capture_install_id": "..." },
  "sync": { "status": "pending", "attempts": 0 }
}
```

Two deliberate departures from the git note: `request_draft` is **false** and
`status` is `"needs_human"` (git notes carry `request_draft: true`, `status:
"staged"`). The `body` preamble is the inverse of the git note's "treat as
evidence" framing.

### Server (the one genuinely new piece)

A sync handler for the `activity-nudge` sink that routes `activity_gap` events to
a **review-inbox surface**, and **excludes them from the auto-draft batch**
(`daily-draft-batch-design.md`) by default. In the review inbox the human can:
dismiss; write a linked note (`lt note`/`lt quick`); or **promote** the window
into real provenance (`lt git snapshot`, attach an artifact) — the moment fuzzy
activity becomes a proper high-provenance link, human-supplied. The nudge itself
never becomes a graph record.

## Privacy & consent

- **Redaction at source**, mirroring `git_capture._strip_url_credentials` (which
  already strips `user:password` from remotes so tokens never enter evidence):
  never store argv/command lines (only the allowlisted tool basename); never
  store contents or paths outside `project_roots` (only a count of distinct files
  touched under the root; basenames only behind `record_basenames`, default
  off); whitelist-only capture.
- **Consent invariants** (inherited from stkn): `lt activity status` and any
  `--dry-run` run unprompted; mutating verbs are `--dry-run`/`--yes`-gated and
  hard-fail non-interactively; local-only until `lt activity sync`;
  `quiet_hours` and `lt activity pause`; `--fail-silent` for scheduler-fired runs.

## Wiring seams (for the implementer)

| Piece | Existing anchor | New |
| --- | --- | --- |
| Envelope builder | `watch.make_event` (`watch.py:512`) | reused |
| Idempotent staging | `watch.write_event` (`:581`), `event_path` (`:602`) | reused |
| Sink constant | `SINK_STAGED_NOTE` (`watch.py:53-55`) | add `SINK_ACTIVITY_NUDGE`, extend `ALLOWED_SINKS` |
| Adapter module | `hpc.py`, `git_capture.py` | `activity.py` |
| CLI group | `hpc_parser` (`cli.py:761`) | `lt activity {init,run,status,sync,pause}` |
| Config file | `.lab-tracker/hpc.json` | `.lab-tracker/activity.json` |
| Scheduler enrollment | `lt setup schedule` (stkn.13, `schedule.py`) | `lt activity run --fail-silent` |
| Server sync | staged-note handler (`watch.py:962`) | `activity-nudge` → review-inbox, excluded from draft batch |

## Testing notes

- Client verbs are flag-driven and JSON-out — testable like `test_watch_cli.py`,
  no new infrastructure.
- New invariants to test: reconciliation **suppresses** a window covered by an
  overlapping outbox commit/artifact/job, and **emits** one event for an
  uncovered window; re-run over the same day is a no-op (idempotent path);
  mutating verbs hard-fail non-interactively without `--yes`/`--dry-run`; argv
  and out-of-root paths never appear in a written event; nudge events carry
  `request_draft:false` and are excluded from the draft batch.

## Open questions (for the scope gate)

1. Does this belong in the product at all, or does the extraction-inbox risk keep
   it deferred indefinitely?
2. Resident sampler vs. cron-only — is a client resident loop acceptable given
   the "no daemon" philosophy, or must this be strictly cron-fired?
3. Foreground-focus sampling vs. process-liveness only — how much fidelity is
   worth the privacy cost?
4. Is the daily digest the right granularity, or should nudges only surface
   inside the existing daily-review batch UI?
