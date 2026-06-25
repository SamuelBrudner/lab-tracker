# Make the daily review run on its own

Lab Tracker's **daily review** gathers your staged captures (notes, photos, voice
memos — including notes tagged as meetings) and proposes how they fit your graph,
in one review queue you accept or reject. It can run **on demand** (the **Run
now** button on the Batches page) or **on a schedule**.

By default Lab Tracker does not run a background scheduler — it's a plain web app,
and you fire the review by pointing a small **external scheduler** at one endpoint,
`POST /batches/run-due`. This page sets that up in one step. If you would rather the
server keep its own clock, there is also an
[opt-in in-process scheduler](#run-it-inside-the-process-opt-in) — pick exactly one
of the two.

> **The model only ever proposes.** The scheduled job triggers *drafting* — a
> human still accepts or rejects every proposal before anything is committed.

---

## Quick start (one command)

Suggested configuration: a local Lab Tracker (`http://127.0.0.1:8000`), polled
every 15 minutes. Each project still fires only at the time *you* set for it.

**Windows** — double-click [`scripts/install-daily-review.cmd`](../scripts/install-daily-review.cmd),
or run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install-daily-review.ps1
```

**macOS / Linux**:

```sh
scripts/install-daily-review.sh
```

That registers a scheduled job (a Windows Scheduled Task, or a `cron` entry) that
nudges the review every 15 minutes. Re-running it just updates the existing job.

### One thing to turn on first

The job does nothing until at least one project has the daily review **enabled**.
Open the **Batches** page at `/app/batches`, pick a project, and set its cadence
(default: daily at **18:00** — early evening — in your timezone, so you confirm
the day's captures before you head out; switch it to `06:00` for a next-morning
review instead). That per-project setting decides when each project actually
runs; the scheduled job is just a frequent, cheap poll.

### Try it without waiting

- **Run now** on the Batches page, or
- run the trigger once yourself:
  `scripts/daily-review-run-due.sh` (or `scripts/daily-review-run-due.ps1`).

Then review the queue at `/app/batches`.

### Remove it

- **Windows:** `Unregister-ScheduledTask -TaskName LabTrackerDailyReview -Confirm:$false`
- **macOS / Linux:** `crontab -l | grep -v '# lab-tracker-daily-review' | crontab -`

---

## Running against a server (with login)

The Quick Start assumes a local instance with authentication disabled, so no
credentials are needed. Two things change when Lab Tracker is **deployed** and
**auth is enabled**:

- **Reachability.** A cloud scheduler (a Claude routine, a Codex automation, a
  hosted cron) can only reach a Lab Tracker that has a public URL. A localhost
  instance must be driven by a scheduler on the **same machine**.
- **Auth.** `run-due` is admin-only. Provide admin credentials via the
  environment; the trigger logs in and mints a fresh short-lived token each run:

  ```sh
  export LAB_TRACKER_BASE_URL="https://lab.example.org"
  export LAB_TRACKER_ADMIN_USER="…"
  export LAB_TRACKER_ADMIN_PASS="…"
  ```

  Pass a non-local URL to the installer with `-BaseUrl` (Windows) or as the
  second argument (`install-daily-review.sh 15 https://lab.example.org`).

---

## Other schedulers

The installers above wrap a one-line trigger you can drive from anything.

### Plain cron / curl

```cron
# Poll every 15 min; per-project run_at_local_time decides the real time.
*/15 * * * * /path/to/lab-tracker/scripts/daily-review-run-due.sh >> ~/.lab-tracker-daily-review.log 2>&1
```

### Claude routine

Create a routine (the `/schedule` skill, or a Claude Code scheduled task) with a
one-line instruction:

> Every 15 minutes, `POST {BASE_URL}/batches/run-due` with the admin bearer token
> and report the run summary. Do not accept or commit any drafts.

Use a **local** scheduled task for a localhost instance; a **cloud** routine only
for a deployed, reachable URL.

### Codex automation

Configure a Codex scheduled automation whose task runs the trigger script
(`scripts/daily-review-run-due.sh`). The repo's `AGENTS.md` already orients Codex
to the project. Same reachability and auth rules as above.

> Whichever you pick, keep the job to **triggering drafts only**. Lab Tracker
> deliberately does not delegate graph commits to autonomous agents — a person
> reviews the queue and commits what they keep.

---

## Run it inside the process (opt-in)

On a single-machine deployment you can skip the external scheduler entirely and let
the server keep its own clock. It is **off by default**; enable it with two settings:

```sh
export LAB_TRACKER_DAILY_REVIEW_IN_PROCESS_SCHEDULER=true
export LAB_TRACKER_DAILY_REVIEW_POLL_SECONDS=900   # optional; defaults to 900 (15 min)
```

When enabled, the process runs the same `run-due` drafting pass on that interval —
no cron, no Scheduled Task, no admin token to manage. The drafting call runs off the
request path, and a failing tick is logged and retried on the next poll rather than
killing the loop.

It carries the same guarantee as every other trigger: a non-interactive **system**
principal that may *draft* but is structurally barred from accepting or committing,
so the in-process scheduler can only ever produce proposals for human review.

Two constraints:

- **Prefer a single worker.** Each worker process starts its own clock. Running
  several is still *correct* — the idempotent `claim_due` stops any project from
  drafting twice — just wasteful, since every worker polls. If you serve with
  multiple workers, run one worker or leave this off and use an external trigger.
- **Pick exactly one trigger.** The in-process scheduler and an external
  cron/Scheduled Task are both safe to run at once — the idempotent `claim_due`
  prevents double-drafting — but running both is needless and confusing, so choose
  one. The external scripts remain the documented fallback and, unlike the in-process
  clock, keep firing across app restarts and survive a crashed process.

---

## How it behaves (why frequent polling is fine)

`POST /batches/run-due` runs every project whose batch settings are enabled and
due, then advances each project's `next_run_at`. It is idempotent: a
compare-and-set (`claim_due`) plus a deterministic `batch_key` mean concurrent or
redundant calls never double-fire a project, and an off-time poll simply finds
nothing due and returns immediately. So the trigger can be dumb and frequent; the
*per-project* cadence does the real scheduling.
