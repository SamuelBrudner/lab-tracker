# Lab Tracker MCP Server

Lab Tracker's MCP server is the supported LLM-facing frontend. It exposes the same domain
service layer as the HTTP app, so MCP clients get the same validation, persistence, and
provenance rules without needing to learn the REST routes.

## Install

```bash
uv pip install -e ".[mcp]"
uv run alembic upgrade head
```

## Run

Local stdio clients:

```bash
uv run lab-tracker-mcp
```

Streamable HTTP clients:

```bash
uv run lab-tracker-mcp --transport streamable-http
```

The streamable HTTP endpoint is `/mcp`, so the default local URL is
`http://127.0.0.1:8000/mcp`.

The server reads the normal `LAB_TRACKER_` settings, including `LAB_TRACKER_DATABASE_URL`,
`LAB_TRACKER_NOTE_STORAGE_PATH`, and `LAB_TRACKER_AUTH_SECRET_KEY`.

MCP calls run as a local actor. By default the actor is admin because local MCP clients are
expected to be trusted automation running on the same machine:

```bash
LAB_TRACKER_MCP_ACTOR_ROLE=editor
LAB_TRACKER_MCP_ACTOR_USER_ID=00000000-0000-0000-0000-000000000000
```

## ChatGPT Web Quick Tunnel

ChatGPT cannot connect to a local stdio server or to `localhost`; it needs a remote HTTPS
MCP endpoint. For local testing from ChatGPT web, use the bundled Cloudflare Tunnel wrapper:

```bash
brew install cloudflared
./scripts/chatgpt-mcp-tunnel.sh
```

The script:

- creates `.venv` if needed
- installs the MCP extra
- runs Alembic migrations
- starts `lab-tracker-mcp --transport streamable-http`
- starts `cloudflared tunnel --url http://127.0.0.1:8000`
- prints the ChatGPT endpoint as `https://...trycloudflare.com/mcp`

The tunnel script defaults `LAB_TRACKER_MCP_ACTOR_ROLE` to `viewer` so the first ChatGPT
connection can inspect context without modifying records. For write testing:

```bash
LAB_TRACKER_MCP_ACTOR_ROLE=editor ./scripts/chatgpt-mcp-tunnel.sh
```

Keep the terminal open while testing; closing it stops both the MCP server and tunnel.
Temporary tunnels are for development only. For regular use, deploy the MCP server behind a
stable HTTPS URL with authentication.

## Client Example

For a stdio MCP client, configure the command as:

```json
{
  "command": "uv",
  "args": ["run", "lab-tracker-mcp"],
  "env": {
    "LAB_TRACKER_DATABASE_URL": "sqlite+pysqlite:///./lab_tracker.db",
    "LAB_TRACKER_AUTH_SECRET_KEY": "dev-only-change-me"
  }
}
```

For HTTP clients, start the server and connect to the SDK default MCP endpoint exposed by
the `streamable-http` transport.

For ChatGPT web, configure a custom MCP app with the HTTPS `/mcp` URL printed by the tunnel
script, or your own deployed HTTPS `/mcp` URL. If the ChatGPT settings UI asks for auth and
you are using the development tunnel, choose no authentication and keep the actor role at
`viewer` unless you are deliberately testing writes.

## Tool Surface

Read and context tools:

- `lab_tracker_overview`
- `lab_tracker_get_project_context`
- `lab_tracker_list_projects`
- `lab_tracker_list_questions`
- `lab_tracker_list_notes`
- `lab_tracker_search`
- `lab_tracker_list_sessions`
- `lab_tracker_list_datasets`
- `lab_tracker_list_analyses`
- `lab_tracker_list_claims`
- `lab_tracker_list_visualizations`

Write tools:

- `lab_tracker_create_project`
- `lab_tracker_create_question`
- `lab_tracker_update_question`
- `lab_tracker_record_note`
- `lab_tracker_start_session`
- `lab_tracker_close_session`
- `lab_tracker_register_acquisition_output`
- `lab_tracker_create_dataset`
- `lab_tracker_commit_dataset`
- `lab_tracker_create_analysis`
- `lab_tracker_commit_analysis`
- `lab_tracker_create_claim`
- `lab_tracker_create_visualization`

Resource and prompt:

- `lab-tracker://project/{project_id}` returns a JSON project context snapshot.
- `lab_tracker_workflow_prompt` gives an LLM a safe operating procedure for preserving
  questions, notes, sessions, datasets, analyses, and claims.

## Operating Notes

- Create staged records when intent is clear but provenance is incomplete.
- Do not invent file paths, checksums, claims, or confidence values.
- Commit datasets only when file manifests and active primary questions are available.
- Keep the React and HTTP surfaces available for humans; MCP is the structured LLM control
  plane.
