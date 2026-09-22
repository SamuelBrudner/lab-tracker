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

### Startup fails importing FastMCP

Lab Tracker currently requires MCP SDK `>=1.27,<2` because the server imports
`mcp.server.fastmcp`. An older Lab Tracker dependency declaration allowed SDK
2.x, which can leave a newly installed `lt-mcp` unable to start.

For a `uv tool` installation, rerun the exact pinned `uv tool install --force`
command shown by Setup, adding `--with "mcp>=1.27,<2"`. This repairs the MCP
dependency while preserving the client revision required by your server. For a
source checkout's virtual environment, run:

```bash
uv pip install "mcp>=1.27,<2"
```

Restart the MCP connection in your assistant, then run the exact
`lt setup verify-mcp --expected-revision <full-revision>` command shown by Setup.
The verifier checks the protocol handshake as well as API health and project
access. Installing MCP 1.x alone does not replace those connection checks.

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

For a private hosted read-only MCP endpoint, enable the compose `mcp` profile:

```bash
export LT_MCP_READONLY_TOKEN=lpat_...
export LT_MCP_INBOUND_TOKEN="$(openssl rand -hex 32)"
docker compose --profile mcp up mcp
```

The service is opt-in, so other compose commands never need these tokens, and
its container refuses to start while either token is empty. It runs the
image's installed MCP server (the `lt-mcp` entry point) with
`LAB_TRACKER_MCP_TRANSPORT=streamable-http`, points the MCP
process at the internal API hop (`http://app:8000`), and publishes only
`127.0.0.1:9000` on the host. Put a private TLS proxy in front of that loopback
port; `deploy/mcp/Caddyfile` is the checked-in example with Origin/Host checks,
Authorization log redaction, and no permissive CORS.

Configure the remote MCP client to send `LT_MCP_INBOUND_TOKEN` as
`Authorization: Bearer <token>` on every request. This random transport secret
must be distinct from the read-only `lpat_` in `LT_MCP_READONLY_TOKEN`; the MCP
process rejects missing or invalid credentials before FastMCP and removes the
header before dispatch. The LPAT is used only for the MCP-to-API hop. Keep the
endpoint private behind TLS or a tailnet; the inbound token is an access gate,
not per-user graph authorization or attribution.

The hosted (`streamable-http`) server is read-only by default and checks that
at startup; it refuses to start (non-zero exit, so a container restart policy
retries) unless:

- `GET /readiness` on the API target succeeds and reports `auth.enabled=true`.
  Unlike stdio, this probe also runs for loopback API targets, and an
  unreachable API, a 404/5xx, or a rejected credential stops startup instead of
  booting unguarded.
- `LAB_TRACKER_MCP_API_KEY` is an `lpat_` token the API refuses to let write.
  The API exposes no token introspection to service tokens, so `lt-mcp` sends an
  empty `POST` to a path no API route serves: the API's token policy answers
  `403 service_forbidden` for a read-only token before any route runs, while a
  write-capable token gets `404`. Only the explicit refusal counts as read-only;
  username/password logins cannot be verified and are refused.

A default hosted server registers only the read tools and resources. Set
`LAB_TRACKER_MCP_ALLOW_WRITES=true` to deliberately serve write tools as well
(the startup read-only check is then skipped and a warning is logged). Even then
the hosted server never registers tools that read files on the MCP host:
`lab_tracker_upload_visualization_file` is absent and
`lab_tracker_record_evidence_bundle` refuses `upload_file`/`upload_file_path`.
Local stdio servers keep the full tool set.

By default the hosted server does not validate `Host` or `Origin`, whatever
`LAB_TRACKER_MCP_HOST` it binds: the inbound bearer already defeats DNS
rebinding, and the reverse proxy in front (the checked-in Caddyfile,
`tailscale serve`, nginx, Traefik) owns the public Host/Origin policy and may
forward the client's `Host` unchanged. To have `lt-mcp` check them too, set a
comma-separated Host allowlist (`:*` matches any port) naming the public host
the proxy forwards, plus any browser origins your clients send:

```bash
LAB_TRACKER_MCP_ALLOWED_HOSTS=mcp.lab.internal
LAB_TRACKER_MCP_ALLOWED_ORIGINS=https://github.com
```

With a Host allowlist set, a request with another `Host` gets `421` and one
whose `Origin` is not listed gets `403` (requests without `Origin` pass).
`LAB_TRACKER_MCP_ALLOWED_ORIGINS` without `LAB_TRACKER_MCP_ALLOWED_HOSTS` is a
startup error. The docker-compose `mcp` service forwards
`LAB_TRACKER_MCP_ALLOW_WRITES`, `LAB_TRACKER_MCP_ALLOWED_HOSTS` and
`LAB_TRACKER_MCP_ALLOWED_ORIGINS` from the ignored `.env`.

For a remote agent, the graph-native read sequence is:

1. `lab_tracker_graph_overview(project_id)` for bounded counts, open entry
   points, and recent nodes.
2. `lab_tracker_search_graph(project_id, query, ...)` for deterministic typed
   hits across questions, notes, sessions, datasets, analyses, claims,
   exploration nodes, goals, and visualizations.
3. `lab_tracker_get_graph_neighborhood(...)` for a capped one- or two-hop
   traversal around one selected anchor.
4. `lab_tracker_get_decision_context(...)` before any research-facing choice.

The graph tools return summaries by default, treat stored text as untrusted,
and expose full anchor text only when explicitly requested, capped at 8,000
characters. They complement rather than replace the existing full project graph
and list/search tools. OKF export is a separate portability concern, not the
interactive traversal path.

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
`LAB_TRACKER_AUTH_ENABLED=true` and set `LAB_TRACKER_AUTH_SECRET_KEY` to a strong
random value when you want to test login, roles, or service account credentials.

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
