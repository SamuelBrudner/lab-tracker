# Set up AI agents for the proposal workflow

This is the end-to-end guide for the person wiring AI into a Lab Tracker
deployment: choosing the drafting model, scheduling the daily review, minting
credentials for automations, and connecting coding agents over MCP. Bench
scientists don't need this page — they capture and review in the app.

**The one rule everything below preserves: AI proposes; only a person
commits.** Agents and schedulers *trigger* drafting, the model *proposes*
graph changes, and a human accepts, edits, or rejects every proposal in the
review queue. The gate is structural, not just policy — non-interactive
principals (the built-in scheduler, service tokens) cannot accept,
bulk-accept, or commit on any code path. See
[`review-and-commit-model.md`](review-and-commit-model.md) and
[`vision.md`](vision.md) for why.

The workflow the pieces add up to:

```
capture (photos, voice, figures, watch folders, commit hooks, agent notes)
   → staged notes
   → drafting (your chosen model proposes typed graph changes,
     each with a rationale, a confidence, and source references)
   → one human review queue (accept / edit / reject / revise-with-AI)
   → committed graph
```

It is deliberately **multimodal**: whiteboard photos and voice memos go to the
model alongside the graph context; voice notes get editable transcripts;
proposals cite their sources down to the region of the image they read; and
you can push back on a draft by typing, dictating, or attaching an image
("revise with AI").

## 1. Choose the drafting provider

The drafting model runs server-side, with server-held keys. **OpenAI,
Anthropic, and Google are equally supported — the choice is yours.** Set
`LAB_TRACKER_GRAPH_DRAFT_PROVIDER` *and* the matching API key; the default
(`openai`) is only a default.

| Provider | `LAB_TRACKER_GRAPH_DRAFT_PROVIDER` | API key variable | Default model | Voice transcription |
| --- | --- | --- | --- | --- |
| OpenAI | `openai` (default) | `LAB_TRACKER_OPENAI_API_KEY` | `gpt-4o-mini` | Yes |
| Anthropic (Claude) | `anthropic` or `claude` | `LAB_TRACKER_ANTHROPIC_API_KEY` | `claude-3-5-sonnet-latest` | No — voice notes need OpenAI or Google |
| Google (Gemini) | `google` or `gemini` | `LAB_TRACKER_GOOGLE_API_KEY` | `gemini-2.5-flash` | Yes |

Per-provider model, base URL, and timeout overrides are in the
[configuration reference](configuration.md#graph-draft-providers-and-transcription).
For institutional deployments, point the provider's base URL at an approved
gateway. An `agentic` provider (a read-only tool-using drafting loop) also
exists and requires the background worker — see
[`server-resident-agentic-drafting-design.md`](server-resident-agentic-drafting-design.md).

An `external_harness` provider is also available for daily-review batch drafting
when a deployment wants Claude Code, Codex CLI, or Gemini CLI to act as the
drafting brain. It is background-worker-only, disabled by default, and should
stay disabled until an operator has established both
`LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS_SANDBOX_PROFILE=operator_managed` and
`LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS_EGRESS_PROFILE=vendor_api_only`.
The harness gets no Lab Tracker token or database setting: it can read only
through the per-run scoped executor MCP surface, with sensitivity forced to
`omit`, and its patch still lands in the same human review queue. The enablement
checklist lives in
[`external-harness-security-review.md`](external-harness-security-review.md).

Two setup facts worth knowing up front:

- A missing key is **not** detected at startup. It surfaces at the first draft
  as a `failed` change set whose error names the variable to set and reminds
  you the provider is switchable.
- The one-click Render deploy does not include any drafting variables — on a
  hosted instance, add the provider and key in the service dashboard before
  expecting drafts.

## 2. Schedule the daily review

Drafting is triggered, never spontaneous. Pick one trigger:

- **Built-in scheduler (preferred on servers):** set
  `LAB_TRACKER_GRAPH_DRAFT_SCHEDULER_ENABLED=true` and the app enqueues due
  reviews itself and drafts them in the background.
- **External scheduler:** cron, launchd, or Windows Task Scheduler polling
  `POST /batches/run-due` — installer scripts included.
- **Your agent platform's automation:** a Claude routine, a Codex scheduled
  automation, or any Gemini-driven job that can run the one-line trigger
  script. Whichever platform you already use is fine; the job only *triggers*
  drafting.

All three paths, with commands and removal instructions, are in
[Make the daily review run on its own](scheduled-daily-review.md). Then enable
a cadence: nothing drafts until at least one project turns the daily review on
at `/app/batches` (per project, or per user within a project). The default is
daily at 18:00 in the cadence row's timezone — which starts as
`America/New_York`, so set yours when you enable it.

## 3. Mint credentials for automations

Mint **personal access tokens** on the **Agents** page in the web app
(`/app/agents`): pick a label, an access level, and an expiry (90-day
maximum), and the page returns the one-time `lpat_…` secret together with
copy-paste setup commands for the machine where the agent runs — an
`lt setup connect --save-token` block for the `lt` client plus the
`LAB_TRACKER_MCP_API_KEY` export MCP agents read. Minting stays
human-in-browser by design: tokens themselves cannot call `/auth/*`, so an
agent can never mint or relay its own credential. (The raw API remains
`POST /auth/tokens` if you script it.)

For `POST /batches/run-due` — which is admin-only — pick the page's
**Scheduler trigger (admin)** level; it stays read-only except for the
run-due trigger. Export the secret as `LAB_TRACKER_API_KEY` next to
`LAB_TRACKER_BASE_URL` for the trigger scripts. Username/password
credentials also work; the trigger script logs in each run.

For agents that should only *read* the graph, pick **Read-only** —
decision-context lookups work read-only, and a read-only principal cannot
stage or draft anything.

## 4. Connect coding agents over MCP

Coding agents reach Lab Tracker exclusively through the MCP server (`lt-mcp`),
which is itself an HTTP client of the API — never the database. Any
MCP-capable agent works; **which one you use is up to you.**

In an analysis repo, one command scaffolds the integration for every major
agent at once:

```bash
lab_tracker init        # or, from the client: lt setup init (--dry-run previews first)
```

| File | Who reads it |
| --- | --- |
| `.mcp.json` | Claude Code and other root-config MCP readers |
| `.cursor/mcp.json` | Cursor |
| `.gemini/settings.json` | Gemini CLI |
| `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` (managed block) | Claude Code, Codex CLI and other AGENTS.md readers, Gemini CLI — the same consultation-policy block in each |
| `.claude/settings.json` | Claude Code hooks (`lt setup status` on session start, `lt prime` before research-facing prompts) |
| `AGENTS.lt.md`, `scripts/lt.py`, `lt_ids.json` | Agent-readable integration notes, the client shim, and the project-id mapping (`lt project bind` fills it) |

Two agents need one extra step:

- **Codex CLI** registers MCP servers in `~/.codex/config.toml`: add
  `[mcp_servers.lab-tracker]` with `command = "lt-mcp"`. (A project-scoped
  `.codex/config.toml` also works, but only in repos the user has marked
  trusted — which is why the scaffold doesn't write one.) It picks up the
  scaffolded `AGENTS.md` per repo.
- **GitHub Copilot** IDEs use a different config schema — see
  [GitHub Copilot MCP setup](lab-tracker-copilot.md); Cursor details are in
  [Cursor MCP setup](lab-tracker-cursor.md).

Point the MCP server at your instance with `LAB_TRACKER_MCP_BASE_URL` (and
`LAB_TRACKER_MCP_API_KEY` when auth is on) — full variable reference in
[`lab-tracker-mcp-skills.md`](lab-tracker-mcp-skills.md).

Every scaffolded instruction file carries the same policy, whatever the vendor: consult
`lab_tracker_get_decision_context` before research-facing decisions; stage
evidence and request drafts only when asked; never accept or commit a draft.
Analysis repos can also send evidence automatically on every commit — see
[analysis graph drafts from CI and git hooks](analysis-graph-drafts-ci.md).

## 5. Verify the loop

1. `lt setup status` in a scaffolded repo — read-only inventory of server
   reachability, profile, scaffold, watches, and hooks.
2. Capture something (phone note, `lt import-folder`, or a figure from code).
3. Press **Run now** on `/app/batches` (or run
   `scripts/daily-review-run-due.sh` / `.ps1`).
4. Open the review queue: proposals should appear with rationale, confidence,
   and source references. A `failed` change set is usually provider
   misconfiguration — read its error metadata; a missing key names the exact
   variable to set.
5. Accept one proposal and commit — as a person, in the app. That's the whole
   loop.

## What agents can and cannot do

| Can | Cannot |
| --- | --- |
| Read decision context, search, list, and walk the graph | Accept, bulk-accept, or commit any draft (structurally blocked for non-interactive principals) |
| Stage evidence notes and figures | Commit datasets/analyses without an explicit user request |
| Trigger or request drafts when the user asks | Bypass review — every accepted operation records *how* it was accepted ([curation states](curation-states.md)) |

The record stays honest about the division of labor: every entity carries an
`origin` (`user` / `ai_suggested` / `ai_executed` / `user_revised`), the change set, provider,
model, and prompt version, all exportable as PROV-O. A rubber-stamped bulk
accept is never mistaken later for a considered per-operation review.
