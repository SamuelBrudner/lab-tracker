# Lab Tracker ChatGPT App MCP Server

Lab Tracker's MCP server is the primary LLM-facing frontend. It exposes a curated
ChatGPT App surface for capture-and-review workflows, backed by the same domain service
layer as the HTTP app. The React app remains a human admin/debug fallback.

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

MCP calls run as a local actor. By default the actor is `viewer` and write tools are not
registered. Enable writes only for deliberate local testing:

```bash
LAB_TRACKER_MCP_ACTOR_USER_ID=00000000-0000-0000-0000-000000000000
LAB_TRACKER_MCP_ACTOR_ROLE=editor
LAB_TRACKER_MCP_ENABLE_WRITES=true
```

Legacy granular tools are hidden by default. Set
`LAB_TRACKER_MCP_EXPOSE_LEGACY_TOOLS=true` only when debugging older MCP clients.

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

The tunnel script defaults to `viewer` with writes disabled so the first ChatGPT connection
can inspect context without modifying records. For write testing:

```bash
LAB_TRACKER_MCP_ACTOR_ROLE=editor LAB_TRACKER_MCP_ENABLE_WRITES=true ./scripts/chatgpt-mcp-tunnel.sh
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
you are using the development tunnel, choose no authentication and keep writes disabled unless
you are deliberately testing local mutations.

## Tool Surface

Read and context tools:

- `lab_context`
- `prepare_lab_note_draft`
- `search_lab_context`
- `refresh_review_dashboard`

Write tools, registered only when `LAB_TRACKER_MCP_ENABLE_WRITES=true` and actor role is
`editor` or `admin`:

- `draft_lab_note_commit`
- `capture_note`
- `stage_question`
- `update_staged_question`
- `activate_question`
- `start_session`
- `close_session`

Resource and prompt:

- `ui://lab-tracker/review-dashboard-v1.html` renders the embedded ChatGPT review dashboard.
- `lab_tracker_workflow_prompt` gives an LLM a safe operating procedure for preserving
  questions, notes, and sessions.

## Uploaded Lab-Note Images

For photographed notebook pages or whiteboard notes, upload the image to ChatGPT and ask it
to transcribe the image. Before converting the transcription into structured records,
ChatGPT should call `prepare_lab_note_draft` with the transcription and a few search terms
from the page. That read-only step queries existing Lab Tracker questions, notes, active
sessions, and dashboard context so ChatGPT can reuse existing IDs and avoid duplicate
questions.

After reviewing the returned candidates, ChatGPT should call `draft_lab_note_commit` with:

- `transcribed_text`: the OCR/transcription text ChatGPT read from the image
- `summary`: a concise interpretation to display in review surfaces
- `source_label`: an optional filename or user-facing description
- `proposed_questions`: optional staged questions extracted from the image
- `target_entity_type` and `target_entity_id`: optional existing context to attach the note to

The MCP server does not store the image bytes in this workflow. It stores only the
LLM-produced transcription/summary as a staged note, creates any proposed questions as
staged questions, and groups the records with a generated `draft_commit_id` for review.
Do not go directly from transcription to structured records without the context lookup.

## Operating Notes

- Create staged records when intent is clear but provenance is incomplete.
- Do not invent file paths, checksums, claims, or confidence values.
- Do not expose delete/archive/dataset commit/analysis commit tools in the default ChatGPT
  App surface.
- Keep the React and HTTP surfaces available for humans; the ChatGPT App is the structured
  LLM control plane.
