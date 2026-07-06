# Make the daily review run on its own

Lab Tracker's **daily review** gathers your staged captures (notes, photos, voice
memos — including notes tagged as meetings) and proposes how they fit your graph,
in one review queue you accept or reject. It can run **on demand** (the **Run
now** button on the Batches page) or **on a schedule**.

On a server deployment, prefer the built-in scheduler/worker: set
`LAB_TRACKER_GRAPH_DRAFT_SCHEDULER_ENABLED=true` and the app will enqueue due
reviews itself, then draft them in the background. The older external scheduler
flow remains supported for hosts that want cron, launchd, Windows Task Scheduler,
or cloud automation to call `POST /batches/run-due`.

> **The model only ever proposes.** The scheduled job triggers *drafting* — a
> human still accepts or rejects every proposal before anything is committed.

This page covers the trigger. For the full agent picture — choosing the
drafting provider (OpenAI, Anthropic, or Google), credentials, and MCP — see
[Set up AI agents for the proposal workflow](agent-setup.md).

---

## Recommended server setup

Enable the server-resident scheduler before starting Lab Tracker:

```sh
export LAB_TRACKER_GRAPH_DRAFT_SCHEDULER_ENABLED=true
```

With that flag on, the app ticks for due reviews, claims eligible cadence rows
safely in the database, enqueues one batch job per reviewer, and runs draft
generation in the worker instead of inside the HTTP request. The **Run now**
button and `POST /batches/run-due` also enqueue work rather than blocking on
model calls.

## External scheduler fallback

Suggested fallback configuration: a local Lab Tracker
(`http://127.0.0.1:8000`), polled every 15 minutes. Each review still fires only
at the time *you* set for it.

**Windows** — double-click [`scripts/install-daily-review.cmd`](../scripts/install-daily-review.cmd),
or run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install-daily-review.ps1
```

**macOS (recommended)** — install a launchd LaunchAgent (survives reboots, runs
in your user session, managed with `launchctl`):

```sh
scripts/install-daily-review-launchd.sh
```

**Linux (or macOS via cron)**:

```sh
scripts/install-daily-review.sh
```

That registers an external job (a Windows Scheduled Task, a launchd LaunchAgent,
or a `cron` entry) that nudges the server every 15 minutes. Re-running it just
updates the existing job. Both \*nix installers take the same optional arguments
(`[interval_minutes] [base_url]`); when auth is enabled they read
`LAB_TRACKER_ADMIN_USER` / `LAB_TRACKER_ADMIN_PASS` from the environment. The
launchd installer writes those credentials to a `0600` file
(`~/.config/lab-tracker/daily-review.env`) that the agent sources at run time,
rather than into the world-readable plist.

### One thing to turn on first

The job does nothing until at least one project has the daily review **enabled**.
Open the **Batches** page at `/app/batches`, pick a project, and set the cadence
for the project default or for a specific user (default: daily at **18:00** —
early evening — so each person confirms the day's captures before heading out;
switch it to `06:00` for a next-morning review instead). The cadence row's
timezone starts as `America/New_York` — set yours when you enable it.
That per-(project, user) setting decides when each review actually runs; the
scheduler is just a frequent, cheap poll.

### Try it without waiting

- **Run now** on the Batches page, or
- run the trigger once yourself:
  `scripts/daily-review-run-due.sh` (or `scripts/daily-review-run-due.ps1`).

With background drafting enabled, those triggers enqueue a pending run and the
worker fills the queue shortly after. Then review the queue at `/app/batches`.

### Remove it

- **Windows:** `Unregister-ScheduledTask -TaskName LabTrackerDailyReview -Confirm:$false`
- **macOS (launchd):** `launchctl bootout gui/$(id -u)/com.lab-tracker.daily-review; rm ~/Library/LaunchAgents/com.lab-tracker.daily-review.plist ~/.config/lab-tracker/daily-review.env`
- **Linux / cron:** `crontab -l | grep -v '# lab-tracker-daily-review' | crontab -`

---

## Running against a server (with login)

The external scheduler fallback assumes a local instance with authentication
disabled, so no credentials are needed. Two things change when Lab Tracker is
**deployed** and **auth is enabled**:

- **Reachability.** A cloud scheduler (a Claude routine, a Codex automation, a
  Gemini-driven job, a hosted cron) can only reach a Lab Tracker that has a
  public URL. A localhost instance must be driven by a scheduler on the
  **same machine**.
- **Auth.** `run-due` is admin-only. Prefer an admin personal access token for
  scheduled automations:

  ```sh
  export LAB_TRACKER_BASE_URL="https://lab.example.org"
  export LAB_TRACKER_API_KEY="lpat_…"
  ```

  Username/password credentials also work; the trigger logs in and mints a
  fresh short-lived token each run:

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
# Poll every 15 min; per-(project, user) run_at_local_time decides the real time.
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

### Gemini CLI

Gemini CLI does not ship its own scheduler, so register the same one-line
trigger with your OS scheduler (the installers above), or run it from any
Gemini-driven automation that can execute `scripts/daily-review-run-due.sh`.
In scaffolded repos, the generated `GEMINI.md` orients Gemini CLI to the
project the same way `AGENTS.md` orients Codex. Same reachability and auth
rules as above.

> Whichever you pick — and whichever agent vendor you prefer — keep the job to
> **triggering drafts only**. Lab Tracker deliberately does not delegate graph
> commits to autonomous agents — a person reviews the queue and commits what
> they keep.

---

## How it behaves (why frequent polling is fine)

`POST /batches/run-due` and the built-in ticker both examine enabled cadence rows
that are due, then advance each row's `next_run_at`. The default project row
partitions staged notes by note author; a user-specific row runs only that user's
new staged notes. Each generated change set keeps `created_by` as the triggering
principal (`SYSTEM` for the built-in scheduler) and sets `review_assignee` to the
person expected to review it.

The operation is idempotent: a compare-and-set (`claim_due`) plus deterministic
batch keys mean concurrent or redundant calls never double-fire the same review,
and an off-time poll simply finds nothing due and returns immediately. So the
trigger can be dumb and frequent; the per-(project, user) cadence does the real
scheduling.
