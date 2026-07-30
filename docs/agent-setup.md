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

For quality-first OpenAI drafting with GPT-5.6 Sol, set:

```dotenv
LAB_TRACKER_OPENAI_MODEL=gpt-5.6-sol
LAB_TRACKER_OPENAI_REASONING_EFFORT=max
LAB_TRACKER_OPENAI_REASONING_MODE=pro
```

These are Responses API settings. Codex Ultra additionally uses agent
orchestration; it is not a valid `reasoning.effort` value and is not enabled by
this configuration.

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

Start on the web app's **Setup** page. It reads the running server's full source
revision and renders `uv tool install` and `uv add` commands pinned to that
immutable revision. If the deployment reports `unknown`, a short hash, or no
revision, Setup stops and asks the operator to correct the deployment metadata;
it never substitutes the moving GitHub `main` branch.

Mint **personal access tokens** on the **Agents** page in the web app
(`/app/agents`): pick a label, an access level, and an expiry (90-day
maximum), and the page returns the one-time `lpat_…` secret together with
copy-paste setup commands for the machine where the agent runs. The
`lt setup connect --save-token` block stores the server URL, selected project,
and token in the permission-hardened local profile used by both `lt` and
`lt-mcp`. Minting stays
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

For figure capture, repository commit hooks, watch folders, or other staged
evidence, pick **Read + stage evidence**. It is the least-privilege writable
choice offered by the app: it can sync staged captures and request drafts, but
non-interactive principals remain structurally unable to accept or commit a
draft. A read-only token cannot drain a capture outbox.

## 4. Connect coding agents over MCP

Coding agents reach Lab Tracker exclusively through the MCP server (`lt-mcp`),
which is itself an HTTP client of the API — never the database. Any
MCP-capable agent works; **which one you use is up to you.**

Install the exact requirement shown by the server in two places:

1. `uv tool install --force "<server-pinned requirement>"` supplies the
   machine-level `lt` and `lt-mcp` executables.
2. Inside each analysis repository, `uv add "<same server-pinned requirement>"`
   records the dependency in that project's Python environment. Confirm with
   the Setup page's `uv run python` import command and
   `uv run lt setup verify-client --expected-revision <full-revision>`.

The tool environment alone is not enough for analysis code that imports
`lab_tracker_client`; the project environment needs its own dependency.

Then, in the analysis repo, one command scaffolds the integration for every
major agent and installs the generated setup skill for both Claude and Codex:

```bash
lt setup init --install-skills --dry-run
lt setup init --install-skills --yes
lt project bind --project-id <selected-project-uuid> --dry-run
lt project bind --project-id <selected-project-uuid> --yes
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
  it with `codex mcp add lab-tracker -- lt-mcp`. Then run
  `lt setup verify-mcp --expected-revision <full-revision>` from the same
  environment that launches Codex. The verifier actually starts `lt-mcp`,
  initializes MCP over stdio, calls Lab Tracker health, and makes an
  authenticated project read through the saved profile. `codex mcp list`
  confirms registration only. A project-scoped `.codex/config.toml` also
  works, but only in repos the user has marked trusted—which is why the
  scaffold does not write one.
- **GitHub Copilot** IDEs use a different config schema — see
  [GitHub Copilot MCP setup](lab-tracker-copilot.md); Cursor details are in
  [Cursor MCP setup](lab-tracker-cursor.md).

The saved connection profile normally supplies the API URL and LPAT. Environment
variables can still override it; see
[`lab-tracker-mcp-skills.md`](lab-tracker-mcp-skills.md).

Server-side AI drafting uses the Lab Tracker operator's configured provider
credential. A researcher connecting `lt` or `lt-mcp` does not need to enter an
OpenAI key locally for Lab Tracker.

Every scaffolded instruction file carries the same policy, whatever the vendor: consult
`lab_tracker_get_decision_context` before research-facing decisions; stage
evidence and request drafts only when asked; never accept or commit a draft.
Analysis repos can also send evidence automatically on every commit — see
[analysis graph drafts from CI and git hooks](analysis-graph-drafts-ci.md).

## 5. Verify the loop

1. `uv run lt setup verify-client --expected-revision <full-revision>` in the
   project environment.
2. `lt setup status` in a scaffolded repo—read-only inventory of server
   reachability, profile, scaffold, skills, watches, and hooks.
3. `lt setup verify-mcp --expected-revision <full-revision>`—a real MCP health
   and authenticated-read check.
4. Capture something (phone note, a hook-generated commit note, or a figure
   from code), then confirm the local outbox syncs.
5. Press **Run now** on `/app/batches` (or run
   `scripts/daily-review-run-due.sh` / `.ps1`).
6. Open the review queue: proposals should appear with rationale, confidence,
   and source references. A `failed` change set is usually provider
   misconfiguration — read its error metadata; a missing key names the exact
   variable to set.
7. Accept one proposal and commit—as a person, in the app. That's the whole
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
