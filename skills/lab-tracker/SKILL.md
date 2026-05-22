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
6. To serve one graph to other computers on a LAN, VPN, or overlay network, use
   `.\scripts\serve-lan.ps1 -UsePostgres` and see `docs/lan-shared-graph.md`.

## MCP Tools

The local MCP server is `python -m lab_tracker.mcp_server`. It calls the running
Lab Tracker API and does not write directly to the database.

MCP environment:

```bash
LAB_TRACKER_MCP_BASE_URL=http://127.0.0.1:8000
LAB_TRACKER_MCP_USERNAME=<service-account-username>
LAB_TRACKER_MCP_PASSWORD=<service-account-password>
```

MCP username/password are only required when `LAB_TRACKER_AUTH_ENABLED=true`.
Local auth-disabled testing can omit them.

Use these tools when available:

- `lab_tracker_get_decision_context` returns bounded graph context before
  research-facing decisions such as choosing plots, analyses, slides, experiment
  plans, summaries, or research writing.
- `lab_tracker_health` checks the API health endpoint.
- `lab_tracker_readiness` checks database and storage readiness.
- `lab_tracker_list_projects` lists active or archived projects.
- `lab_tracker_list_questions` filters questions by project, status, type, search text,
  direct parent, or recursive ancestor.
- `lab_tracker_list_notes` filters notes by project, status, or search text.
- `lab_tracker_list_sessions` lists sessions by project, status, or type.
- `lab_tracker_list_datasets` lists datasets by project or status.
- `lab_tracker_list_analyses` lists analyses by project, dataset, question, or status.
- `lab_tracker_list_claims` lists claims by project, status, dataset, or analysis.
- `lab_tracker_list_visualizations` lists visualizations by project, analysis, or claim.
- `lab_tracker_get_dataset_provenance` returns dataset provenance JSON-LD.
- `lab_tracker_get_analysis_provenance` returns analysis provenance JSON-LD.
- `lab_tracker_search` searches questions and notes together.
- `lab_tracker_create_project` creates a local project.
- `lab_tracker_create_question` creates a question in a project; pass
  `parent_question_ids` to place atomic child questions under broader motivating
  questions.
- `lab_tracker_create_note` creates a text note in a project. Note statuses are
  `staged`, `committed`, and `archived`; do not use question statuses such as
  `active`. Metadata values may be strings, numbers, or booleans and are stored
  as strings; nested metadata objects and arrays are unsupported.

Creation tools write through the API, using the configured service account when
authentication is enabled. Be explicit before creating or mutating research
records.

Before research-facing decisions, use `lab_tracker_get_decision_context` when
available. This includes choosing variables to plot, analyses to run, figures or
slides to make, experimental controls to prioritize, summaries to write, and
research writing such as manuscripts, grants, abstracts, results, discussion
text, and figure legends. If Lab Tracker is unavailable or ambiguous, state that
explicitly before proceeding.

For MCP clients on other computers, point `LAB_TRACKER_MCP_BASE_URL` at the
serving machine, for example `http://<host-ip>:8000` or a Tailscale tailnet URL.
Use the LAN helper script or `docs/lan-shared-graph.md` to find the URL and
firewall command.

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
- Use `parent_question_ids` as the v1 hierarchy mechanism: broad motivating
  questions should sit above small atomic experimental, method, control, and
  analysis questions.
- Sessions capture acquisition activity and can promote outputs into datasets.
- Notes are raw human records and can target projects, questions, sessions, datasets,
  analyses, claims, or visualizations. Notes use `staged`, `committed`, and
  `archived` status, not question `active` status.
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
