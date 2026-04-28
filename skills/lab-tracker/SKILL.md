---
name: lab-tracker
description: Use when working with the Lab Tracker application, API-backed MCP server, Postgres live runtime, or Dolt mirror. Covers project/question/note/session/dataset/analysis/claim/visualization workflows, retained-v1 product boundaries, local startup, validation, and MCP tool usage.
allowed-tools: "Read,Bash(uv:*),Bash(python:*),Bash(pytest:*),Bash(npm:*),Bash(bd:*)"
version: "0.1.0"
compatible-with: claude-code,codex
tags: [lab-tracker, research-data, mcp, fastapi, sqlalchemy]
---

# Lab Tracker

Lab Tracker preserves the reasoning around lab work: projects, questions, acquisition
sessions, datasets, notes, analyses, claims, and visualizations. Treat the app as a
research-context system, not a generic file manager.

## First Moves

1. Read `README.md` and `docs/retained-v1-surface.md` for current product scope.
2. Use `bd ready` and `bd show <id>` for tracked work.
3. For multi-client work, prefer Postgres through `docker compose up postgres` and
   set `LAB_TRACKER_DATABASE_URL` to
   `postgresql+psycopg://lab_tracker:lab_tracker@127.0.0.1:5432/lab_tracker`.
4. Run `uv run alembic upgrade head` before using a fresh database.
5. Use `uv run uvicorn lab_tracker.asgi:app --reload` to serve the app at
   `http://127.0.0.1:8000/app`.

## MCP Tools

The local MCP server is `python -m lab_tracker.mcp_server`. It calls the running
Lab Tracker API and does not write directly to the database.

Required MCP environment:

```bash
LAB_TRACKER_MCP_BASE_URL=http://127.0.0.1:8000
LAB_TRACKER_MCP_USERNAME=<service-account-username>
LAB_TRACKER_MCP_PASSWORD=<service-account-password>
```

Use these tools when available:

- `lab_tracker_health` checks the API health endpoint.
- `lab_tracker_readiness` checks database and storage readiness.
- `lab_tracker_list_projects` lists active or archived projects.
- `lab_tracker_list_questions` filters questions by project, status, type, or search text.
- `lab_tracker_list_notes` filters notes by project, status, or search text.
- `lab_tracker_search` searches questions and notes together.
- `lab_tracker_create_project` creates a local project.
- `lab_tracker_create_question` creates a question in a project.
- `lab_tracker_create_note` creates a text note in a project.

Creation tools write through the API using the configured service account. Be explicit
before creating or mutating research records.

## Dolt Mirror

Dolt is an export-only versioned mirror for snapshots, diffs, branches, and later
remote sync. The live API database remains the source of truth.

```bash
python -m lab_tracker.dolt_mirror export --message "Lab Tracker snapshot"
```

Defaults: `.lab-tracker-dolt/` for the local mirror and `dolt` for the executable.
Use `LAB_TRACKER_DOLT_BIN` or `LAB_TRACKER_DOLT_MIRROR_PATH` to override them.

## Domain Cues

- Questions are first-class and may be staged, active, answered, or abandoned.
- Sessions capture acquisition activity and can promote outputs into datasets.
- Notes are raw human records and can target projects, questions, sessions, datasets,
  analyses, claims, or visualizations.
- Datasets preserve provenance through commit manifests.
- Analyses, claims, and visualizations should stay linked back to their source
  datasets and questions.

## Quality Gates

Backend:

```bash
uv run pytest -q
uv run ruff check .
```

Frontend, when `src/lab_tracker/frontend_src` or the committed bundle changes:

```bash
npm run test:frontend
npm run lint:frontend
npm run build
```

## Boundaries

The retained-v1 runtime is defined by `docs/retained-v1-surface.md`. Deferred ideas
from `idea.md` should not be treated as active product requirements unless a bead
explicitly says to implement them.
