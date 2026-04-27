# Coding-Agent MCP And Skill Support

Lab Tracker ships a read-only coding-agent MCP profile plus a portable skill. Use this
surface from Claude Code, Codex, or another coding assistant when source changes should be
informed by live Lab Tracker project, question, note, or session context.

ChatGPT App setup remains in [`docs/mcp.md`](mcp.md).

## Install

```bash
uv pip install -e ".[mcp]"
uv run alembic upgrade head
```

The coding profile is read-only by design. It ignores `LAB_TRACKER_MCP_ENABLE_WRITES` and
does not expose ChatGPT widget resources, legacy CRUD tools, or write tools.

## Claude Code MCP

From the repo root:

```bash
claude mcp add --transport stdio --scope local lab-tracker-coding -- uv run lab-tracker-coding-mcp
```

If you use a non-default database or storage location, pass the same `LAB_TRACKER_`
environment variables you use for the API server.

## Generic MCP Config

Use this shape for Codex or other stdio MCP clients:

```json
{
  "mcpServers": {
    "lab-tracker-coding": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "lab-tracker-coding-mcp"],
      "env": {
        "LAB_TRACKER_DATABASE_URL": "sqlite+pysqlite:///./lab_tracker.db",
        "LAB_TRACKER_AUTH_SECRET_KEY": "dev-only-change-me"
      }
    }
  }
}
```

The same server can be started explicitly with:

```bash
uv run lab-tracker-mcp --profile coding
```

## Portable Skill

The portable skill lives at `agent-skills/lab-tracker-coding/SKILL.md`.

Claude Code personal install:

```bash
mkdir -p ~/.claude/skills
ln -s "$PWD/agent-skills/lab-tracker-coding" ~/.claude/skills/lab-tracker-coding
```

Codex personal install:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
ln -s "$PWD/agent-skills/lab-tracker-coding" \
  "${CODEX_HOME:-$HOME/.codex}/skills/lab-tracker-coding"
```

Copy the folder instead of symlinking if your client cannot follow symlinks. The skill tells
coding agents to read `AGENTS.md`, inspect repo docs, query the read-only coding MCP profile
when live lab context matters, and preserve the ChatGPT App MCP surface unless asked.
