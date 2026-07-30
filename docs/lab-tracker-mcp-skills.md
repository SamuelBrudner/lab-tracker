# Lab Tracker MCP, Skills, and Dolt Mirror

Lab Tracker uses the API as the single write path for browser users, MCP clients,
scripts, and future workers. For multi-client work, run the app against Postgres
and point assistant MCP clients at the running API.

## API-Backed MCP Server

Run the MCP server with:

```bash
lt-mcp
```

The installed `lt-mcp` console script is the canonical portable launch command.
`python -m lab_tracker.mcp_server` remains supported for source checkouts and
manual debugging.

Environment for read/write tools:

```bash
LAB_TRACKER_BASE_URL=http://127.0.0.1:8000
LAB_TRACKER_MCP_API_KEY=<lpat-personal-access-token>
LAB_TRACKER_MCP_USERNAME=<service-account-username>
LAB_TRACKER_MCP_PASSWORD=<service-account-password>
```

For agents that are not running on the graph workstation, use the current
workstation HTTPS base URL:

```bash
LAB_TRACKER_BASE_URL=https://lab-tracker.example.org
LAB_TRACKER_MCP_API_KEY=<read-only-lpat-token>
LAB_TRACKER_MCP_USERNAME=<service-account-username>
LAB_TRACKER_MCP_PASSWORD=<service-account-password>
```

The server does not store bearer tokens. When `LAB_TRACKER_MCP_API_KEY` (or
`LAB_TRACKER_MCP_TOKEN`) is set, the client sends that `lpat_` token directly
and does not call `/auth/login`. Otherwise it logs in with the configured
username/password and retries once after a 401. Credentials are only required
when `LAB_TRACKER_AUTH_ENABLED=true`; local auth-disabled testing can omit them.

For a private hosted read-only MCP endpoint, use the compose `mcp` service:

```bash
LT_MCP_READONLY_TOKEN=lpat_... docker compose up mcp
```

It runs `lt-mcp` with `LAB_TRACKER_MCP_TRANSPORT=streamable-http`, points the MCP
process at the internal API hop (`http://app:8000`), and publishes only
`127.0.0.1:9000` on the host. Put a private TLS proxy in front of that loopback
port; `deploy/mcp/Caddyfile` is the checked-in example with Origin/Host checks,
Authorization log redaction, and no permissive CORS.

Portable consumer `.mcp.json` files should use the console entry point rather
than a hardcoded absolute Python path:

```json
{
  "mcpServers": {
    "lab-tracker": {
      "command": "lt-mcp",
      "env": {
        "LAB_TRACKER_BASE_URL": "http://127.0.0.1:8000",
        "LAB_TRACKER_MCP_API_KEY": "<lpat-personal-access-token>",
        "LAB_TRACKER_MCP_USERNAME": "<service-account-username>",
        "LAB_TRACKER_MCP_PASSWORD": "<service-account-password>"
      }
    }
  }
}
```

For clients that deliberately launch from a source checkout instead of an
installed environment, keep the path portable by using an environment-provided
interpreter:

```json
{
  "mcpServers": {
    "lab-tracker": {
      "command": "${LAB_TRACKER_PYTHON:-python}",
      "args": ["-m", "lab_tracker.mcp_server"],
      "env": {
        "LAB_TRACKER_BASE_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

The authoritative MCP tool inventory is generated in
[`skills/lab-tracker/SKILL.md`](../skills/lab-tracker/SKILL.md) from the
registered `READ_TOOLS` and `WRITE_TOOLS` tuples. Do not duplicate the list in
this document; run `python scripts/generate_lab_tracker_skill_reference.py` after
changing MCP tool registration.

Decision-context tooling for assistant clients is specified in
[`docs/mcp-decision-context-tooling.md`](mcp-decision-context-tooling.md). That
tooling lets assistants request bounded graph context before choosing plots,
analyses, slides, experiment plans, summaries, or research writing.

`lab_tracker_get_decision_context` accepts `task_kind` values `plot`,
`analysis`, `slides`, `experiment_plan`, `summary`, `research_writing`, and
`progress_review`.
It returns bounded project graph context, task guidance, stable IDs, relevance
reasons, an evidence map, truncation metadata, and a `write_front_door` block
with resolved project scope, anchor IDs, candidate entity IDs, allowed task
kinds, and guidance for follow-on create calls. If the request is ambiguous, for
example because no project can be inferred, it returns a structured error
instead of guessing. Use it before research-facing read-then-write tasks.

The MCP tool calls the Lab Tracker API endpoint `POST /assistant/decision-context`;
the API remains the single context-building path for browser users, MCP clients,
scripts, and future workers.

`lab_tracker_describe_schema` calls `GET /schema/describe` and returns
source-derived metadata for entity create/update fields, required fields,
allowed enum values, and known status lifecycle transitions. Pass `entity_type`
such as `question`, `dataset`, `claim`, `visualization`, or `goal` to filter the
response.

`lab_tracker_list_questions` can traverse the v1 question hierarchy with
`parent_question_id` for direct children or `ancestor_question_id` for recursive
descendants. `lab_tracker_create_question` accepts `parent_question_ids`; use it
to place small atomic experimental, method, control, and analysis questions under
broader motivating questions.

`lab_tracker_create_note` creates text notes. Note status is note-specific:
allowed values are `staged`, `committed`, and `archived`; do not use question
statuses such as `active`. Note metadata accepts an object whose values are
strings, numbers, or booleans, and Lab Tracker normalizes those values to strings
when storing the note. Nested metadata objects and arrays are not supported. Pass
`targets` as a list of `{entity_type, entity_id}` objects to attach a source note
to the most specific relevant graph record.

## Evidence Authoring

Agents should read existing questions, datasets, analyses, claims,
visualizations, and notes before creating evidence records. Reuse existing
records when they already represent the source, analysis, claim, or figure.

Create or reuse datasets before analyses. Create analyses before supported claims
or visualizations. Attach source notes to the most specific relevant entity, such
as a claim or visualization instead of only the project. Use `supported` claim
status only when `supported_by_dataset_ids` or `supported_by_analysis_ids` is
present; use `proposed` for human interpretation without concrete supporting
records. Verify the final graph with the list tools.

For retrospective literature evidence, staged datasets are acceptable
placeholders for source collections such as dissertation analyses or published
recording sets. Prefer real method hashes and code versions when available; use
stable publication labels such as `publication:eLife-2021-vae-feature-space` and
`published-pdf:elife-67855-v2` when source hashes are unavailable. Prefer real
local artifact paths for visualization `file_path`; use DOI or PDF figure
locators such as `doi:10.1371/journal.pcbi.1011051#fig5` only when no local plot
file exists.

`lab_tracker_record_evidence_bundle` is the composite MCP authoring helper for
one result. `dry_run` defaults to `true` and returns a reviewable plan with
proposed creates, reused records, warnings, and idempotency behavior. Component
objects use the existing flat MCP shape; an entity ID selects an existing record,
while create fields request a new one. With `dry_run=false`, a non-blank
`idempotency_key` is required and the graph records are committed through one
strict atomic bundle endpoint. An identical principal-scoped replay returns the
same stable IDs; reusing the key with conflicting fields returns `409` rather
than matching records semantically. Provide concrete dataset manifest/hash,
analysis `method_hash`/`code_version`, claim text/confidence, and visualization
path or upload details. Local visualization files are snapshotted and
fingerprinted before the atomic command, then uploaded as an explicit
client-side follow-up; attachment failure cannot roll back an already committed
graph bundle and is reported as such.

## Postgres Runtime

For multiple live clients, prefer Postgres:

```powershell
docker compose up postgres
$env:LAB_TRACKER_DATABASE_URL = "postgresql+psycopg://lab_tracker:lab_tracker@127.0.0.1:5432/lab_tracker"
uv run alembic upgrade head
uv run uvicorn lab_tracker.asgi:app --reload
```

Or run the full app stack:

```bash
docker compose up app
```

SQLite remains the local fallback for simple single-client development.

Local development starts with authentication disabled. Set
`LAB_TRACKER_AUTH_ENABLED=true` when you want to test login, roles, or service
account credentials.

For MCP clients on other computers, use the reachable shared-server URL instead
of localhost, for example:

```powershell
$env:LAB_TRACKER_BASE_URL = "http://<host-or-tailnet-ip>:8000"
```

For off-network agents, prefer the durable Tailscale Funnel endpoint:

```powershell
$env:LAB_TRACKER_BASE_URL = "https://lab-tracker.example.org"
```

See [`docs/lan-shared-graph.md`](lan-shared-graph.md) for same-LAN, VPN, and
tailnet-only access.

## Dolt Mirror

Dolt is an export-only versioned mirror in v1. The live API database remains the
source of truth.

```bash
python -m lab_tracker.dolt_mirror export --message "Lab Tracker snapshot"
```

Defaults:

- Mirror path: `.lab-tracker-dolt/`
- Dolt binary: `dolt`
- Override binary with `LAB_TRACKER_DOLT_BIN`
- Override mirror path with `LAB_TRACKER_DOLT_MIRROR_PATH`

The exporter mirrors retained Lab Tracker tables, including graph draft review
tables, and excludes `users`.

## Skill

The skill source lives at:

```text
skills/lab-tracker/SKILL.md
```

On this machine it should be installed into both assistant homes, preferably as
symlinks so repo updates are picked up by new agent sessions:

```text
~/.codex/skills/lab-tracker -> <repo>/skills/lab-tracker
~/.claude/skills/lab-tracker -> <repo>/skills/lab-tracker
```

Restart Codex or Claude after changing MCP or skill config so the new server and
skill are loaded.
