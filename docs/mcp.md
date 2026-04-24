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

The server reads the normal `LAB_TRACKER_` settings, including `LAB_TRACKER_DATABASE_URL`,
`LAB_TRACKER_NOTE_STORAGE_PATH`, and `LAB_TRACKER_AUTH_SECRET_KEY`.

MCP calls run as a local actor. By default the actor is admin because local MCP clients are
expected to be trusted automation running on the same machine:

```bash
LAB_TRACKER_MCP_ACTOR_ROLE=editor
LAB_TRACKER_MCP_ACTOR_USER_ID=00000000-0000-0000-0000-000000000000
```

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
