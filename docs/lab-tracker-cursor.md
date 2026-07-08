# Cursor MCP Setup

Lab Tracker exposes an MCP server for Cursor. The supported first mode is local
stdio: Cursor starts `lt-mcp` on your machine, and that process talks to the Lab
Tracker API configured in its environment.

Keep the server local. For shared use, expose only the private, read-only hosted
endpoint described below — never publish the write-capable stdio server to the
public internet.

## Config Shape

Cursor reads MCP config from two places, both using the top-level `mcpServers`
shape:

- `.cursor/mcp.json` in a project directory (project-scoped)
- `~/.cursor/mcp.json` (global, applies to every workspace)

This is the **same shape as `.mcp.json`**, not Copilot's `servers` schema.
Cursor does **not** auto-read the root `.mcp.json`, and it does not use the
VS Code `inputs` / `${input:...}` prompt mechanism that `.vscode/mcp.json` uses.
So Cursor needs its own `.cursor/mcp.json`.

This repo includes `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "lab-tracker": {
      "command": "lt-mcp",
      "env": {
        "LAB_TRACKER_MCP_BASE_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

Start Lab Tracker locally, open this repository in Cursor, then enable the
`lab-tracker` server from Cursor Settings → MCP (use the refresh control if it
does not appear immediately). The default base URL is `http://127.0.0.1:8000`.

`lab_tracker init` writes the same `.cursor/mcp.json` into consumer repos, so
Cursor users get the server out of the box there too.

## Authentication

If local auth is disabled, the committed config works as-is — no credentials
needed.

If auth is enabled, prefer a personal access token: mint one on the **Agents**
page in the web app (`/app/agents`, which also prints ready-made setup
commands), or with `POST /auth/tokens`, then provide the returned
`lpat_...` secret as
`LAB_TRACKER_MCP_API_KEY`. The token secret is returned once; Lab Tracker stores
only its SHA-256 hash, and the MCP client sends it as `Authorization: Bearer ...`
without calling `/auth/login`. Username/password
(`LAB_TRACKER_MCP_USERNAME` / `LAB_TRACKER_MCP_PASSWORD`) still works as a
fallback.

Cursor's `mcp.json` has no interactive secret prompt, so keep secrets out of the
committed project file. Put them in one of:

- the global `~/.cursor/mcp.json` `env` block (not in the repo), or
- the environment Cursor inherits when it launches `lt-mcp`.

On macOS, a GUI-launched Cursor may not inherit your shell's exported
environment, so the global `~/.cursor/mcp.json` `env` block is usually the most
reliable place for both credentials and any `PATH` adjustments.

## `lt-mcp` on PATH

Cursor must be able to find `lt-mcp`. The installed console script is the
preferred portable command. The most reliable way to get it (and `lt`) onto
`PATH` for a GUI editor is a global uv tool install, which puts them in a stable
`~/.local/bin` (`%USERPROFILE%\.local\bin` on Windows):

```bash
uv tool install "git+https://github.com/SamuelBrudner/lab-tracker"
uv tool update-shell   # first time only, if ~/.local/bin isn't on PATH yet
```

If you would rather not install it, either point `command` at the absolute path
of the script in your environment (e.g. `<venv>/bin/lt-mcp`), or run it through
`uvx` against the repository:

```json
{
  "mcpServers": {
    "lab-tracker": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/SamuelBrudner/lab-tracker",
        "lt-mcp"
      ],
      "env": {
        "LAB_TRACKER_MCP_BASE_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

## Private Hosted Read-Only Endpoint

For a shared lab endpoint, run the optional compose MCP service with a read-only
`lpat_` token:

```bash
LT_MCP_READONLY_TOKEN=lpat_... docker compose up mcp
```

The service starts Lab Tracker MCP with `LAB_TRACKER_MCP_TRANSPORT=streamable-http`
and publishes it on host loopback as `127.0.0.1:9000` by default. Put a private
TLS proxy or `tailscale serve` in front of that loopback port; do not expose it
publicly. The example proxy config lives at `deploy/mcp/Caddyfile` and strips
Authorization from logs while rejecting unrecognized Origin/Host values.

The hosted mode is read-only by construction: use a token issued with
`read_only=true` and a viewer role. Write tools remain present for local stdio
clients, but the API denies writes made through that hosted token.

## Conventions vs. MCP

`.cursor/mcp.json` connects the tools; `.cursor/rules/lab-tracker.mdc` (written
by `lab_tracker init --yes`) carries the code-facing conventions Cursor applies
as project rules. They are independent: the rules file does not configure the
server, and the MCP config does not carry conventions.

## What Cursor May Do

Read tools are annotated as read-only, and destructive graph edits are marked
destructive. Cursor gates tool calls behind its own approval flow; keep
destructive and write tools behind explicit approval.

The product rule is unchanged: AI can suggest; only a person commits.
