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
bulk-accept, or commit on any code path. The one exception is a grant a
project owner makes on purpose: **delegated curation** lets the server's own
drafting pass, and agents holding a **Curate graph (delegated)** token, apply
the proposals the grant admits without waiting for review, each recorded as
`auto_accepted` so the graph never mistakes it for a considered review. It
is off until an owner turns it on. See
[`delegated-curation.md`](delegated-curation.md),
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
Voice transcription remains a manual action unless the operator explicitly
sets `LAB_TRACKER_AUTO_TRANSCRIBE_VOICE_CAPTURES=true`. That opt-in sends every
new audio upload and its optional capture hint to the configured OpenAI or
Google provider in a fail-soft background task. It is disabled by default,
incurs provider usage, and currently has no per-principal rate limit or daily
budget.
For institutional deployments, point the provider's base URL at an approved
gateway.

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

Email cues appear only when the server reports that delivery is configured.
When delivery is unavailable or the capability is missing, the app says so,
hides the opt-in fields, and saves email notifications disabled with no
destination. This prevents schedules from accumulating an undeliverable email
backlog.

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
evidence, pick **Read + stage evidence**. Its API scope is `stage_evidence`
(`POST /auth/tokens` with `"scope": "stage_evidence"`), which the Agents page
sets for that level. The scope is an exact allow-list applied before routing,
plus two body-level rules the routes enforce:

- every read (`GET`, `HEAD`, `OPTIONS`) and the two semantic-read POSTs,
  `/assistant/decision-context` and `/external-artifacts/resolve`;
- `POST /notes`, `/notes/upload-file`, and `/notes/quick-capture` with the
  `staged` status only — `status=committed` is refused with
  `403 service_forbidden`;
- `PATCH /notes/{id}` for the transcript, targets, and metadata — except
  `status=committed`, refused the same way;
- `POST /notes/{id}/graph-drafts`, `/notes/{id}/analysis-graph-drafts`, and
  `/notes/{id}/transcript`;
- `POST /evidence-bundles` with `dry_run=true` only — a commit is refused with
  `403 service_forbidden`;
- nothing else: no other create or patch, no archive or delete, no
  `/batches/run-due`, and no `/auth/*`.

The writes above additionally require a write-enabled token with the editor or
admin role; a read-only or viewer token keeps only the reads. It is the
least-privilege writable choice: it can sync staged captures and request
drafts, it cannot create a committed record at all, and outside a
delegated-curation grant non-interactive principals remain structurally unable
to accept or commit a draft. A read-only token cannot drain a capture outbox.

For an agent that should also *organize* the graph on its own, pick
**Curate graph (delegated)**. Its API scope is `graph_curate`: everything
`stage_evidence` allows, plus `POST /batches/run-now`, accepting proposals
(`PATCH /graph-drafts/{id}/operations/{op}` with `status=accepted`, `POST
/graph-drafts/{id}/accept-all`), and `POST /graph-drafts/{id}/commit`. The
routes are open to the token; whether an accept or commit goes through is
decided per proposal against the project's delegated-curation grant (see
[`delegated-curation.md`](delegated-curation.md)): with the grant off every
accept and commit is refused with `403`, `organize` admits the link
proposals whose payload carries nothing but the link, `full` admits every
valid proposal except a clarification request. The ordinary review rules
still apply first: the token's user must be the draft's author or assigned
reviewer (or a global admin) to touch it at all.
The token's user must be a project owner to commit, may only *accept* (never
edit, reject, or defer), and every accept it makes is recorded as
`auto_accepted` against that user. Turning the grant on is the owner's act in
the app; the token cannot widen it.

When a commit made during an agent task reports a Lab Tracker timeout, sync
failure, or queued event, the agent must treat the outcome as ambiguous because
the server may already have accepted the capture. Before reporting it as
unresolved, run `lt outbox sync` from that repository, then run
`lt outbox status`. Replay is idempotent and deduplicates an already accepted
capture. Report an unresolved capture only if the retry fails and status still
shows pending events.

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

Binding searches all projects visible to the configured credentials. If an ID
cannot be found, run `lt setup status` to check the server URL, then confirm the
ID and project membership with the project owner. A successful `lt health`
checks connectivity, not authenticated project access. `--dry-run` previews the
binding without writing `lt_ids.json`.

The generated setup skill is machine-wide, not per repo. When `lt setup status`
reports it missing or stale, `lt update --skills-only` (`--dry-run` previews)
refreshes it in the Claude and Codex homes and touches no file in the current
directory, so it is safe to run outside an analysis repo (a relative
`LAB_TRACKER_SKILLS_HOME` resolves against the current directory, so keep that
override absolute). The `lt setup init` step above is for onboarding a repo.

The Agent access page verifies the selected project's effective membership with
the newly issued token before displaying connection, binding, or capture
commands. If an admin's token lacks membership, **Grant project access** adds
the admin's account as a contributor (for capture) or viewer (for read-only use)
and checks the same token again. Failed checks keep the token available to copy
and provide a retry without requiring another token. Scheduler-only tokens do
not offer repository setup commands.

Personal tokens use their own role: an admin account's editor/viewer token still
needs direct project membership or inherited group access. If the browser shows
the project but the token cannot find it, have an owner add the token's user as
a project contributor (or viewer for read-only use).

| File | Who reads it |
| --- | --- |
| `.mcp.json` | Claude Code and other root-config MCP readers |
| `.cursor/mcp.json` | Cursor |
| `.gemini/settings.json` | Gemini CLI |
| `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` (managed block) | Claude Code, Codex CLI and other AGENTS.md readers, Gemini CLI — the same consultation-policy block in each |
| `.claude/settings.json` | Claude Code hooks (`lt setup status` on session start, `lt prime` before research-facing prompts; the opt-in `lt setup agent-hooks` entries go in the personal `.claude/settings.local.json` unless `--shared`) |
| `AGENTS.lt.md`, `scripts/lt.py`, `lt_ids.json` | Agent-readable integration notes, the client shim, and the project-id mapping (`lt project bind` fills it) |
| `.cursor/rules/lab-tracker.mdc` (only with `--yes`) | Cursor (the managed code-conventions block that `--yes` also adds to `CLAUDE.md` and `AGENTS.md`) |

### Choose your client

Registration is per client, and each client reads a different file.
`lt setup init` writes the repository files in the table above, and the
`.mcp.json` it writes carries no token. That covers Claude Code. Any run without
`--dry-run` also records the repository in `~/.lab-tracker/applied-repos.json`,
and `--install-skills` additionally writes the generated setup skill into the
user-level Claude and Codex skill homes (`~/.claude/skills` and
`~/.agents/skills`). It never writes a client's own MCP registration file
(`claude_desktop_config.json`, `~/.codex/config.toml`, or `~/.claude.json`), so
Claude Desktop chat and both Codex products are registered by hand, once per
machine. Follow only the section for the client you use; the steps for another
client do not apply to it.

| Client | Registered through | Written by `lt setup init` |
| --- | --- | --- |
| Claude Code (terminal, IDE, and the Claude Desktop app's Code tab) | the repository `.mcp.json`, or `claude mcp add` | yes, `.mcp.json` |
| Claude Desktop chat | you, in `claude_desktop_config.json` | no |
| Codex in the ChatGPT desktop app | you, in the app's **Settings**, or `~/.codex/config.toml` | no |
| Codex CLI | you, with `codex mcp add`, or `~/.codex/config.toml` | no |

Two rules apply in this section. The first covers the registrations you make by
hand, Claude Desktop chat and both Codex products; the second covers all four
clients. Cursor and GitHub Copilot follow their own pages, linked at the end of
this section, and those pages set out their own credential placement.

**No credentials in the Claude Desktop or Codex settings file.** The saved
connection profile (`~/.lab-tracker/config.json`, written by
`lt setup connect --save-token`) is permission-hardened, and `lt-mcp` reads the API
URL and token from it when a host launches it without a shell. Keep the token
there. For Claude Desktop chat and both Codex products, never paste an `lpat_`
token or `LAB_TRACKER_MCP_API_KEY` into `claude_desktop_config.json` or the Codex
`config.toml`. Two caveats follow from how `lt-mcp` merges its settings:

- A `LAB_TRACKER_BASE_URL` in the entry or the environment that differs from the
  profile's base URL makes `lt-mcp` drop the profile token, because a saved token
  is never sent to a different server. Leave `env` out of a desktop entry. To
  reach another instance, run `lt setup connect` for it instead.
- A variable exported only in a shell startup file, such as
  `LAB_TRACKER_BASE_URL` or `LAB_TRACKER_CONFIG_DIR`, may not reach a desktop
  app. Without `LAB_TRACKER_CONFIG_DIR`, `lt-mcp` looks for the profile in the
  default `~/.lab-tracker` directory.

**Verify in three parts.** Registration alone proves little, so each section
below ends with the same three checks:

1. **Registration check.** The client's own listing shows the server. This does
   not prove that authentication works.
2. **Launch check.** `lt setup verify-mcp --expected-revision <full-revision>`
   starts `lt-mcp` over stdio, initializes MCP, calls Lab Tracker health, and
   makes an authenticated project read through the saved profile. It runs in
   your terminal's environment and, by default, prefers the `lt-mcp` next to the
   `lt` you ran, so a pass shows that the executable and the profile work from a
   terminal, not what a desktop app launched. For a desktop app, add
   `--command <the absolute path you registered>`.
3. **In-client read.** Ask the assistant to call `lab_tracker_list_projects` with
   `limit` 1. It should return a project, or an empty list when you belong to
   none, rather than an authentication error. Only this check exercises exactly
   what the client started.

#### Claude Code (terminal, IDE, and the Desktop app Code tab)

This covers `claude` in a terminal, Claude Code in an IDE, and the Code tab of
the Claude Desktop app. Anthropic's documentation says the Code tab reads the
same `.mcp.json` and `~/.claude.json` configuration as the command line.

Prerequisites: `lt` and `lt-mcp` installed as described above, a saved connection
profile (with a token when the server requires authentication), and the repository
you are onboarding. The `claude mcp` commands need the `claude` command line on
your `PATH`; Anthropic notes that installing the VS Code extension does not put it
there, so use `/mcp` in the extension's chat panel instead.

Register, from the analysis repository:

```bash
lt setup init --install-skills --dry-run
lt setup init --install-skills --yes
```

The dry run previews the files; running without `--dry-run` writes them, and
`--yes` additionally consents to the managed code-conventions blocks in
`CLAUDE.md`, `AGENTS.md`, and `.cursor/rules/lab-tracker.mdc`. Among the files
written is `.mcp.json`, whose `lab-tracker` server runs `lt-mcp` with only
`LAB_TRACKER_BASE_URL` in its environment and no token. Open `claude` in that
repository and approve the server
when prompted. Claude Code asks for approval in an interactive session before it
uses a project-scoped `.mcp.json` server, and a cloned repository cannot approve
its own servers. Until you approve it, `claude mcp list` shows the server as
`Pending approval`. To reach Lab Tracker outside this repository, register it for
your user account instead:
`claude mcp add --transport stdio --scope user lab-tracker -- lt-mcp`.
Start a new session after registering.

Verify:

1. Registration check: `claude mcp list` shows `lab-tracker` connected rather
   than pending approval, or run `/mcp` inside a session.
2. Launch check: `lt setup verify-mcp --expected-revision <full-revision>`.
3. In-client read: ask Claude Code to call `lab_tracker_list_projects` with
   `limit` 1.

#### Claude Desktop chat

Claude Desktop chat is supported by manual registration only: `lt` never writes
`claude_desktop_config.json`. A chat is not tied to an analysis repository, so the
repository features (commit hooks, watch folders, autotrack, `lt run`) are not
part of this route; the Lab Tracker MCP tools are what it provides.

Prerequisites: Claude Desktop, `lt-mcp` installed with `uv tool install` as
described above, and a saved connection profile (with a token when the server
requires authentication).

Register:

1. Find the absolute path of `lt-mcp`: `command -v lt-mcp` in a macOS or Linux
   shell, or `(Get-Command lt-mcp).Source` in PowerShell. `uv tool dir --bin`
   prints the directory `uv tool` installs executables into.
2. In Claude Desktop, open **Settings**, then **Developer**, then **Edit Config**.
   The MCP project's guide lists the file as
   `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS and
   `%APPDATA%\Claude\claude_desktop_config.json` on Windows.

Then add the entry, keeping any servers already in the file. If `mcpServers`
exists, add only the `lab-tracker` key inside it. Use the absolute path from
step 1 and no `env` block:

```json
{
  "mcpServers": {
    "lab-tracker": {
      "command": "<absolute path to lt-mcp>"
    }
  }
}
```

On Windows the command typically ends in `lt-mcp.exe`, and every backslash is
doubled in JSON. Finally, completely quit Claude Desktop and reopen it.

The absolute path is a precaution. Anthropic's Desktop page says, about local Code
sessions, that the app "does not always inherit your full shell environment", and
the MCP project's guide asks that file paths in this file be absolute, so an
absolute `command` removes any doubt about whether the app can find `lt-mcp`. The
same Desktop page says the app loads the servers in this file into local Code tab
sessions, and uses this file's definition when `.mcp.json` or `~/.claude.json`
names the same server.

Verify:

1. Registration check: after reopening, open the connectors list (the guide
   describes it under the conversation input's "Add files, connectors, and more"
   control) and check that `lab-tracker` and its tools appear. The logs are
   `mcp.log` and `mcp-server-lab-tracker.log` in `~/Library/Logs/Claude` on macOS
   and `%APPDATA%\Claude\logs` on Windows. `lt auth doctor` lists the
   registration it finds in this file and flags deprecated username and password
   credentials; it does not launch the server. For an entry without `env`,
   `lt auth doctor` reports auth mode `none` and no base URL. That is expected:
   the token and URL come from the saved profile, which `lt auth doctor` does not
   read. Do not add credentials to the entry to change that result.
2. Launch check:
   `lt setup verify-mcp --expected-revision <full-revision> --command <absolute path from step 1>`.
3. In-client read: ask Claude to call `lab_tracker_list_projects` with `limit` 1.
   Claude Desktop may ask you to approve the tool call.

#### Codex in the ChatGPT desktop app

OpenAI's MCP documentation calls this product the ChatGPT desktop app and says it,
the Codex CLI, and the IDE extension share MCP configuration for the same Codex
host, stored in `~/.codex/config.toml`. The steps below use the app's
**Settings** (or that shared file) rather than the `codex` command.

Prerequisites: the ChatGPT desktop app with Codex, `lt-mcp` installed with
`uv tool install` as described above, a saved connection profile (with a token
when the server requires authentication), and the absolute path of `lt-mcp`
(`command -v lt-mcp`, or `(Get-Command lt-mcp).Source` in PowerShell).

Register: open **Settings**, select **MCP servers**, then **Add server**. Name it
`lab-tracker`, choose **STDIO**, and enter the absolute path of `lt-mcp` as the
command. Leave arguments and environment empty. Save the server, then select
**Restart**. OpenAI's documentation does not describe the environment the desktop
app gives a STDIO server, so the absolute path is a precaution rather than a
requirement. Because the configuration is shared, the equivalent entry in
`~/.codex/config.toml` is:

```toml
[mcp_servers.lab-tracker]
command = "<absolute path to lt-mcp>"
```

On Windows, write the path as a single-quoted TOML literal string, such as
`command = 'C:\Users\<user>\bin\lt-mcp.exe'`, or keep the double quotes and double
every backslash, as in `command = "C:\\Users\\<user>\\bin\\lt-mcp.exe"`. A
double-quoted string with single backslashes does not parse (`\U` starts a unicode
escape), and because `~/.codex/config.toml` is shared by the Codex products, one bad
path makes the whole file invalid TOML, not only this entry.

Verify:

1. Registration check: type `/mcp` in the composer to view connected servers.
2. Launch check:
   `lt setup verify-mcp --expected-revision <full-revision> --command <the absolute path you registered>`.
3. In-client read: ask Codex to call `lab_tracker_list_projects` with `limit` 1.

#### Codex CLI

Use this section only if you run `codex` in a terminal. The `codex` command comes
from the Codex CLI, which has its own install steps on
[OpenAI's Codex CLI page](https://learn.chatgpt.com/docs/codex/cli) (a shell
installer, npm, or Homebrew), and it must be on your `PATH` before `codex mcp`
works. A shell that answers `command not found: codex` (for example
`zsh: command not found: codex`) cannot find the executable: either the CLI is
not installed there, or it is installed in a directory that is not on `PATH`.
Install it, or add its directory to `PATH`. OpenAI's
[environment-variable page](https://learn.chatgpt.com/docs/config-file/environment-variables)
lists `~/.local/bin` as the default install directory of the standalone installer
on macOS and Linux. `uv tool dir --bin` prints the directory that holds `lt` and
`lt-mcp`, and `uv tool update-shell` ensures that directory is on your shell's
`PATH`. If you use the desktop app instead, follow the previous section; it does
not need this.

Prerequisites: the `codex` command on `PATH`, `lt-mcp` installed with
`uv tool install` as described above, and a saved connection profile (with a
token when the server requires authentication).

Register: `codex mcp add lab-tracker -- lt-mcp`. A project-scoped
`.codex/config.toml` also works, but only in repos you have marked trusted, which
is why the scaffold does not write one. OpenAI documents `codex mcp add`,
`codex mcp list`, the desktop **Settings** route above, and a hand-edited
`config.toml` side by side on its
[Codex MCP page](https://learn.chatgpt.com/docs/extend/mcp), so use that page for
the current steps.

Verify:

1. Registration check: `codex mcp list`, or `/mcp` in the `codex` terminal
   interface. This confirms registration only.
2. Launch check: `lt setup verify-mcp --expected-revision <full-revision>`, run in
   the same terminal environment that launches `codex`.
3. In-client read: ask Codex to call `lab_tracker_list_projects` with `limit` 1.

#### Cursor and GitHub Copilot

**GitHub Copilot** IDEs use a different config schema — see
[GitHub Copilot MCP setup](lab-tracker-copilot.md). Cursor details, including
what to do when a GUI-launched Cursor cannot find `lt-mcp`, are in
[Cursor MCP setup](lab-tracker-cursor.md).

#### All clients

Official references: [Claude Code MCP](https://code.claude.com/docs/en/mcp),
[Claude Code in the Desktop app](https://code.claude.com/docs/en/desktop),
[Connect to local MCP servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
for Claude Desktop, and [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp).

The saved connection profile normally supplies the API URL and LPAT. For a client
that a shell launches, environment variables still override it when you need
them — `LAB_TRACKER_BASE_URL` points the MCP server at your instance, and
`LAB_TRACKER_MCP_API_KEY` supplies the token when auth is on — but keep them out
of the Claude Desktop and Codex settings files, as above. Full variable reference in
[`lab-tracker-mcp-skills.md`](lab-tracker-mcp-skills.md).

Server-side AI drafting uses the Lab Tracker operator's configured provider
credential. A researcher connecting `lt` or `lt-mcp` does not need to enter an
OpenAI key locally for Lab Tracker.

Every scaffolded instruction file carries the same policy, whatever the vendor: consult
`lab_tracker_get_decision_context` before research-facing decisions; stage
evidence and request drafts only when asked; never accept or commit a draft
yourself, except through the delegated-curation tools when the user asks and
the server admits it (a Curate graph token in a project whose owner granted
delegation).
Analysis repos can also send evidence automatically on every commit — see
[analysis graph drafts from CI and git hooks](analysis-graph-drafts-ci.md).

### Optional: capture agent sessions

`lt setup agent-hooks` adds two Claude Code hooks that `lt setup init` never
installs, because they capture agent conversations. A `SessionEnd` hook runs
`lt agent session-end`, which stages one bounded, redacted retrospective of
each session in a bound checkout (the person's prompts, files edited, commands,
test and lint outcomes, the final message; never the transcript) and asks for
drafts, so the decisions, dead ends, and pivots it describes reach the review
queue as proposals. A `PostToolUse` hook on `Write|Edit|MultiEdit|NotebookEdit`
runs `lt watch touch` in the background, queuing a file the agent writes into a
configured watch folder right away. Preview with `--dry-run`, apply with
`--yes`, remove with `--uninstall --yes`. The hooks go into the personal
`.claude/settings.local.json` (keep it in `.gitignore`), so they capture only
the sessions of the person who opted in; `--shared` writes the usually
committed `.claude/settings.json` instead and warns that everyone who clones
the repository with `lt` configured would then have their sessions captured.
`lt setup status` reports whether they are installed, in either file.
Cursor and Codex have comparable hook points but are not supported; details,
limits, and the `LAB_TRACKER_AGENT_HOOKS=0` kill switch are in
[agent session capture](agent-session-capture.md).

Agents can also offer the other capture paths that need no code changes:
figure autotrack, `lt run`, pipeline capture, and `lt capture file`. The
`lab-tracker://setup-guide` MCP resource and the managed conventions block
describe them, and the [capture guide](capture-guide.md) maps all of them.

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
| Read decision context, search, list, and walk the graph | Accept, bulk-accept, or commit any draft (structurally blocked for non-interactive principals), except a `graph_curate` token acting under the project owner's delegated-curation grant |
| Stage evidence notes and figures | With a `stage_evidence` or `graph_curate` token: create a dataset, analysis, claim, question, goal, or visualization, commit a note, or commit an evidence bundle — the scope has no route for it |
| Trigger or request drafts when the user asks (`lab_tracker_request_graph_draft`, `lab_tracker_run_graph_draft_batch`), list their own review queue (`lab_tracker_list_my_drafts`), and read a draft (`lab_tracker_get_graph_draft`) | Bypass review — every accepted operation records *how* it was accepted ([curation states](curation-states.md)); a delegated accept is `auto_accepted`, never `human_selected` |
| With a `graph_curate` token in a project whose owner delegated curation: accept the proposals the grant admits (`lab_tracker_accept_graph_draft_operations`) and commit once nothing is left for a person (`lab_tracker_commit_graph_draft`) | Edit, reject, defer, or submit a proposal, review member-onboarding drafts, or widen the grant — all of these stay a person's acts |
| With an `all`-scope writable token: create canonical records directly, declaring `origin` as `user` or `ai_executed` | Write anonymously: every service-token write stamps the token label as `origin_provider` |

The record stays honest about the division of labor: every entity carries an
`origin` (`user` / `ai_suggested` / `ai_executed` / `user_revised`), the change set, provider,
model, and prompt version, all exportable as PROV-O. Every write made with a
personal access token records the token's label as the entity's
`origin_provider`, truncated to the column's 80 characters, whatever `origin`
the request declared; PROV-O exports attribute an `ai_executed` record to a
per-entity `prov:SoftwareAgent` carrying that label, next to the person it is
attributed to. A rubber-stamped bulk accept is never mistaken later for a
considered per-operation review.

## Diagnose an unavailable connection

Run `lt setup status` to inspect `server.reachable`. Failed probes also return
`diagnosis`, `detail`, and `next_step`. `lt setup connect --base-url <url>`
(with `--dry-run` or `--yes`) runs the same probe: it keeps `server_reachable`
and adds a `server_diagnostic` object with those three fields, plus
`status_code` for `http_error`, whenever a diagnosis exists. The probe has
2-second connect and read timeouts and a 4-second deadline on the whole response
(see [setup.md](setup.md#know-when-a-client-install-is-broken-or-behind-its-server)
for what that deadline does not cover), and it observes the actual request; it
makes no extra network probes and does not require the Tailscale CLI. MCP transport
failures expose the same `diagnosis` and `next_step` while preserving their
fail-soft `proceed_without_graph_context` action.

| Diagnosis | Observation and next step |
|---|---|
| `dns_resolution_failed` | Name resolution failed; check the hostname and resolver. |
| `tcp_connection_failed` | TCP could not connect; check the address, listener, routing, and firewall. |
| `tls_handshake_stalled` | TCP connected, but TLS timed out; ask the operator to inspect the HTTPS listener or reverse proxy. |
| `tls_handshake_failed` | TCP connected, but the TLS handshake failed without timing out; check the host's HTTPS listener and proxy TLS configuration. |
| `proxy_connection_failed` | The connection to the configured HTTP proxy failed; check the client's proxy settings and the proxy service. |
| `tls_certificate_error` | Certificate verification failed; check the hostname, certificate, clock, and CA configuration. Do not disable verification. |
| `http_response_timeout` | The connection was established, but an HTTP response timed out; inspect application/proxy logs. |
| `http_error` | The health endpoint returned HTTP 4xx/5xx; inspect its status and application/proxy configuration. |
| `transport_error` | The transport did not supply enough evidence to identify the stage. |

For a `.ts.net` address, a TLS stall includes conditional Funnel guidance:
on the **Lab Tracker host**, check `tailscale funnel status`, `tailscale status`,
and the service listening on its proxied port. An offline Funnel origin is one possible cause,
not something a client can prove from the timeout alone. Public Funnel clients
do not need to join the tailnet. DNS resolution and a successful TCP connection
do not prove that the origin is serving. A TLS stall is not the same as an HTTP
502 from a stopped backend. In one recorded incident a stopped backend behind a
working Funnel returned a 502 after the handshake completed; in another, a
stalled handshake cleared after the Tailscale node was reconnected, though its
cause was not confirmed. Two incidents are not a rule and not a diagnosis of
your instance. For a host-side checklist, including what to test from outside
the tailnet, see
[Publishing Through Tailscale Funnel](self-hosted-operations.md#publishing-through-tailscale-funnel).

For compatibility, `reachable` remains true for HTTP responses below 500,
including authentication errors; it describes connectivity, not token validity
or project access. These diagnostics require an updated local Lab Tracker client
or MCP process, so upgrade the client and restart the MCP host after installing.
