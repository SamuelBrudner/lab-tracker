# GitHub Copilot MCP Setup

Lab Tracker exposes an MCP server for GitHub Copilot in human-driven IDEs. The
supported first mode is local stdio: the IDE starts `lt-mcp` on your machine,
and that process talks to the Lab Tracker API configured in its environment.

The autonomous github.com coding agent is out of scope for this setup. Do not
publish the MCP server to the public internet for that agent path. For hosted
use, keep any endpoint private and read-only.

## VS Code

This repo includes `.vscode/mcp.json`, which uses GitHub Copilot's top-level
`servers` schema:

```json
{
  "servers": {
    "lab-tracker": {
      "type": "stdio",
      "command": "lt-mcp"
    }
  }
}
```

Start Lab Tracker locally, open this repository in VS Code, and enable the
`lab-tracker` MCP server from Copilot's MCP server list. The default base URL is
`http://127.0.0.1:8000`.

If local auth is disabled, leave the prompted token, username, and password
blank. If auth is enabled, prefer a personal access token: mint one on the
**Agents** page in the web app (`/app/agents`), or with `POST /auth/tokens`,
paste the returned `lpat_...` secret into the token prompt, and leave
username/password blank. Username/password still works as a fallback.

The token secret is returned once. Lab Tracker stores only its SHA-256 hash, and
the MCP client sends it as `Authorization: Bearer ...` without calling
`/auth/login`.

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

## Visual Studio

Visual Studio discovers the same `.vscode/mcp.json` file and top-level `servers`
shape as VS Code, so no second root-level config is needed. Keep `.mcp.json` for
clients that expect the top-level `mcpServers` shape.

## Source Checkout Fallback

The installed console script is the preferred portable command:

```json
{
  "servers": {
    "lab-tracker": {
      "type": "stdio",
      "command": "lt-mcp",
      "env": {
        "LAB_TRACKER_MCP_BASE_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

For a machine without `lt-mcp` on `PATH`, use `uvx` against the repository:

```json
{
  "servers": {
    "lab-tracker": {
      "type": "stdio",
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

## What Copilot May Do

Read tools are annotated as read-only so Copilot IDEs can run them without a
confirmation dialog. Write tools are not read-only, and destructive graph edits
are marked destructive so the IDE prompts before use.

The product rule is unchanged: AI can suggest; only a person commits.
