# Scheduling the daily review

Lab Tracker's "daily review" is the human-gated **graph-draft batch**: it windows
over your staged notes (including notes tagged as meetings, see below), asks the
model to propose graph changes — for meeting notes, to *flesh out the scientific
content* (questions, follow-ups, claims) — and lands them in a change set that a
human accepts or rejects. Nothing commits automatically.

Lab Tracker does **not** run an in-process scheduler. The app is a plain FastAPI
server; firing the review on a cadence is the job of an **external scheduled
trigger** that calls one endpoint:

```
POST /batches/run-due        # admin-only, no body
```

`run-due` runs every project whose batch settings are **enabled and due**,
advances each project's `next_run_at`, and is idempotent: a compare-and-set
(`claim_due`) plus a deterministic `batch_key` mean it is safe to call from more
than one trigger, and a redundant call simply finds nothing due. So the trigger
can be dumb and frequent; the *per-project* settings decide when work actually
happens.

> **Retained-v1 guardrail.** The scheduled job triggers **drafting only**. The
> resulting change set still requires a human to edit/accept/reject before
> anything is committed. Do not wire a job that auto-accepts or auto-commits —
> Lab Tracker does not delegate graph commits to autonomous agents.

## 1. Configure per-project cadence (once)

`run-due` only fires projects whose batch settings are enabled. Set this per
project, either in the UI (the **Batches** page at `/app/batches`) or via the
API:

```
PATCH /projects/{project_id}/graph-draft-batch-settings
{
  "enabled": true,
  "cadence_minutes": 1440,          // daily
  "run_at_local_time": "06:00",
  "timezone_name": "America/New_York"
}
```

`run_at_local_time` + `timezone_name` decide *when each day* a project becomes
due. Because of that, the external trigger should **poll on a short interval**
(every 15–30 minutes is plenty) rather than once a day — each project fires near
its configured local time, and off-time polls find nothing due and cost nothing.

## 2. Tag a note as a meeting (so the draft fleshes out its science)

A note becomes a "meeting" by carrying the free-form metadata key
`note_type=meeting`. Any draft over that note gets a meeting-framed instruction
and the **Batches** banner shows *"A meeting is waiting to be fleshed out."*
(There is not yet a one-click "mark as meeting" control in the capture UI —
tracked as a follow-up — so for now set it on the note's `metadata`.)

## 3. Pick a scheduler

Reachability decides which fits:

| Where Lab Tracker runs | Use |
| --- | --- |
| Local dev (`127.0.0.1:8000`) | A **local** scheduler: OS cron / Task Scheduler, or a **local** Claude Code scheduled task / Codex automation. Cloud routines cannot reach localhost. |
| Deployed / reachable URL | Any of the below, including **cloud** Claude routines and Codex automations, pointed at the public base URL with an admin token. |

### Auth

- **Auth disabled** (local default, `LAB_TRACKER_ENVIRONMENT=local` with auth
  off): the local principal is admin — no token needed.
- **Auth enabled** (deployed): obtain an admin bearer token, then call `run-due`
  with it. Access tokens are short-lived (`LAB_TRACKER_AUTH_TOKEN_TTL_MINUTES`,
  default 12h), so the job should log in each run rather than cache a token.

### Variant A — cron + curl (portable baseline)

```bash
#!/usr/bin/env bash
set -euo pipefail
BASE="${LAB_TRACKER_BASE_URL:-http://127.0.0.1:8000}"

# Auth-enabled deployments: mint a fresh admin token each run.
if [ -n "${LAB_TRACKER_ADMIN_USER:-}" ]; then
  TOKEN=$(curl -fsS -X POST "$BASE/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$LAB_TRACKER_ADMIN_USER\",\"password\":\"$LAB_TRACKER_ADMIN_PASS\"}" \
    | jq -r '.data.access_token')
  AUTH=(-H "Authorization: Bearer $TOKEN")
else
  AUTH=()   # local, auth disabled
fi

curl -fsS -X POST "$BASE/batches/run-due" "${AUTH[@]}"
```

```cron
# Poll every 15 minutes; per-project run_at_local_time decides the real time.
*/15 * * * * /usr/local/bin/lab-tracker-run-due.sh >> /var/log/lab-tracker-review.log 2>&1
```

On Windows, register the same script with Task Scheduler on a 15-minute
repeating trigger.

### Variant B — Claude routine (scheduled cloud agent)

Create a routine with the `/schedule` skill (or a Claude Code scheduled task).
Two shapes:

- **Thin** (recommended, matches this design): the routine's prompt is just
  *"POST `$BASE/batches/run-due` with the admin bearer token and report the run
  summary."* Equivalent to Variant A, scheduled by Claude.
- **Agentic** (future, out of scope here): a routine that uses the lab-tracker
  MCP to find the day's meeting notes, trigger drafting, and surface a richer
  prompt. Still must stop at drafts — no auto-commit.

Use a **local** scheduled task for a localhost instance; a **cloud** routine only
for a deployed, reachable Lab Tracker.

### Variant C — Codex automation

Configure a Codex scheduled automation whose task runs the Variant A trigger
(the repo's `AGENTS.md` already orients Codex to the project). Same reachability
and auth rules apply: local automation for localhost, remote for a deployed URL.

## Manual / testing

To fire one project immediately without waiting for its cadence (owner-gated):

```
POST /batches/run-now
{ "project_id": "<uuid>" }
```

Then open `/app/batches` to review and accept/reject the draft.
