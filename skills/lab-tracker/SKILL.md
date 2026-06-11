---
name: lab-tracker
description: Use when working with the Lab Tracker application, API-backed MCP server, Postgres live runtime, Dolt mirror, consumer Python client, or consumer repo scaffolding. Covers project/question/note/session/dataset/analysis/claim/visualization workflows, retained-v1 product boundaries, local startup, validation, and MCP tool usage.
allowed-tools: "Read,Bash(uv:*),Bash(python:*),Bash(pytest:*),Bash(npm:*),Bash(bd:*)"
version: "0.1.0"
compatible-with: claude-code,codex
tags: [lab-tracker, research-data, mcp, fastapi, sqlalchemy]
---

# Lab Tracker

Lab Tracker preserves the reasoning around lab work: projects, questions,
acquisition sessions, datasets, notes, analyses, claims, and visualizations.
Treat the app as a research-context system, not a generic file manager.

## First Moves

1. Read `README.md` and `docs/retained-v1-surface.md` for current product scope.
2. Use `bd ready` and `bd show <id>` for tracked repo work.
3. For multi-client work, prefer Postgres through `docker compose up postgres`
   and set `LAB_TRACKER_DATABASE_URL` to
   `postgresql+psycopg://lab_tracker:lab_tracker@127.0.0.1:5432/lab_tracker`.
4. Run `uv run alembic upgrade head` before using a fresh database.
5. Use `uv run uvicorn lab_tracker.asgi:app --reload` to serve the app at
   `http://127.0.0.1:8000/app`.
6. To serve one graph to other computers on a LAN or VPN, use
   `.\scripts\serve-lan.ps1 -UsePostgres` and see `docs/lan-shared-graph.md`.
7. On Sam's current workstation, Lab Tracker is served durably through Tailscale
   Funnel at `https://mwcppc01ysbc155.tail79f9d8.ts.net/app`.

## Consumer Repos

Use `lab_tracker init --target <repo>` to scaffold portable consumer integration
files. The generated `scripts/lt.py` is a thin shim over `lab_tracker_client`,
and the generated `.mcp.json` uses the portable `lt-mcp` command instead of a
workstation-specific Python path.

For substantive, rerunnable notes, prefer `lab_tracker_client.LabTracker` or
the generated `scripts.lt.upsert_note(...)`. Notes are idempotent by the first
non-blank line of `content`; treat that first line as a stable marker.

## MCP Tools

The local MCP server is `lt-mcp`. `python -m lab_tracker.mcp_server` remains
supported for source checkouts. The MCP server calls the running Lab Tracker API
and does not write directly to the database.

MCP environment:

```bash
LAB_TRACKER_MCP_BASE_URL=http://127.0.0.1:8000
LAB_TRACKER_MCP_USERNAME=<service-account-username>
LAB_TRACKER_MCP_PASSWORD=<service-account-password>
```

For agents running somewhere other than this workstation, use the public
Tailscale Funnel base URL instead of localhost:

```bash
LAB_TRACKER_MCP_BASE_URL=https://mwcppc01ysbc155.tail79f9d8.ts.net
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
- `lab_tracker_list_questions` filters questions by project, status, type,
  search text, direct parent, or recursive ancestor.
- `lab_tracker_list_notes` filters notes by project, status, search text, or
  target.
- `lab_tracker_list_sessions` lists sessions by project, status, or type.
- `lab_tracker_list_datasets` lists datasets by project or status.
- `lab_tracker_list_analyses` lists analyses by project, dataset, question, or
  status.
- `lab_tracker_list_claims` lists claims by project, status, dataset, or
  analysis.
- `lab_tracker_list_visualizations` lists visualizations by project, analysis,
  or claim.
- `lab_tracker_get_dataset_provenance` returns dataset provenance JSON-LD.
- `lab_tracker_get_analysis_provenance` returns analysis provenance JSON-LD.
- `lab_tracker_search` searches questions and notes together.
- `lab_tracker_create_project` creates a project.
- `lab_tracker_create_question` creates a question in a project; pass
  `parent_question_ids` to place atomic child questions under broader motivating
  questions.
- `lab_tracker_create_note` creates a text note in a project. Note statuses are
  `staged`, `committed`, and `archived`; do not use question statuses such as
  `active`. Pass `targets` to attach notes to projects, questions, sessions,
  datasets, analyses, claims, visualizations, or other notes. Metadata values may
  be strings, numbers, or booleans and are stored as strings; nested metadata
  objects and arrays are unsupported.
- `lab_tracker_create_dataset` creates a dataset linked to a primary question
  and optional secondary questions.
- `lab_tracker_create_analysis` creates an analysis linked to one or more
  datasets.
- `lab_tracker_create_claim` creates a claim, optionally linked to supporting
  datasets or analyses.
- `lab_tracker_create_visualization` creates a visualization linked to an
  analysis and optional related claims.
- `lab_tracker_upload_visualization_file` uploads a local file into managed Lab
  Tracker storage for a visualization node. Use this when a plot or extracted
  figure exists on disk and should be available to remote clients through the
  API rather than only by local filesystem path.

Creation tools write through the API, using the configured service account when
authentication is enabled. Be explicit before creating or mutating research
records.

## Evidence Authoring

Before creating evidence records, read the existing questions, datasets,
analyses, claims, visualizations, and notes for the project. Reuse existing graph
records when they already represent the source or result.

Author evidence in this order:

1. Create or reuse datasets before creating analyses.
2. Create analyses before creating supported claims or visualizations.
3. Attach source notes to the most specific relevant entity, such as a claim or
   visualization rather than only the project.
4. Use `supported` claim status only when `supported_by_dataset_ids` or
   `supported_by_analysis_ids` is present. Use `proposed` for human
   interpretation without a concrete supporting record.
5. Prefer managed visualization uploads for plot or figure assets that exist on
   disk. Keep `file_path` as a source locator when useful, then call
   `lab_tracker_upload_visualization_file` so the graph node exposes an API
   download path and checksum. For retrospective paper figures, use DOI or PDF
   locators such as `doi:10.1371/journal.pcbi.1011051#fig5` only when no local
   plot file exists.
6. Verify the final graph with list tools.

For retrospective literature evidence, staged datasets are acceptable
placeholders for source collections such as dissertation analyses or
published-recording sets. Prefer real method hashes and code versions when
available; otherwise use stable publication labels such as
`publication:eLife-2021-vae-feature-space` and
`published-pdf:elife-67855-v2`.

Before research-facing decisions, use `lab_tracker_get_decision_context` when
available. This includes choosing variables to plot, analyses to run, figures or
slides to make, experimental controls to prioritize, summaries to write, and
research writing such as manuscripts, grants, abstracts, results, discussion
text, and figure legends. If Lab Tracker is unavailable or ambiguous, state that
explicitly before proceeding.

For MCP clients on other computers, point `LAB_TRACKER_MCP_BASE_URL` at the
serving machine, preferably the durable HTTPS Funnel URL above. Same-tailnet or
LAN-only clients can also use `http://<host-ip>:8000` when the server is
explicitly bound for LAN serving. Use `docs/workstation-https-serving.md` and
`docs/lan-shared-graph.md` for the current serving modes.

## Question Staging Workflow

Use Lab Tracker as the question/reasoning layer, not the execution task tracker.
For new projects or newly imported repo context:

1. Create or find the project.
2. Draft candidate question hierarchies with `status: "staged"`.
3. Read back the staged queue with `lab_tracker_list_questions status="staged"`
   so the user can review wording, hierarchy, type, and hypothesis.
4. Activate only approved questions through the app or API
   `PATCH /questions/{question_id}` with `{"status": "active"}`.
5. Keep concrete execution tasks in the repo issue tracker when the repo has one;
   link their results back as notes, analyses, datasets, or conclusions.

Question status transitions are one-way for review: `staged` can become
`active` or `abandoned`, but `active` cannot return to `staged`.

## Dolt Mirror

Dolt is an export-only versioned mirror for snapshots, diffs, branches, and
later remote sync. The live API database remains the source of truth.

```bash
python -m lab_tracker.dolt_mirror export --message "Lab Tracker snapshot"
```

Defaults: `.lab-tracker-dolt/` for the local mirror and `dolt` for the
executable. Use `LAB_TRACKER_DOLT_BIN` or `LAB_TRACKER_DOLT_MIRROR_PATH` to
override them.

## Domain Cues

- Questions are first-class and may be staged, active, answered, abandoned, or
  superseded.
- Use `parent_question_ids` as the v1 hierarchy mechanism: broad motivating
  questions should sit above small atomic experimental, method, control, and
  analysis questions.
- Sessions capture acquisition activity and can promote outputs into datasets.
- Notes are raw human records and can target projects, questions, sessions,
  datasets, analyses, claims, visualizations, or notes. Notes use `staged`,
  `committed`, and `archived` status, not question `active` status.
- Datasets preserve provenance through commit manifests.
- Analyses, claims, and visualizations should stay linked back to their source
  datasets and questions.

<!-- BEGIN GENERATED API REFERENCE -->
## API Fields And Enums (Generated)

Generated from the FastAPI OpenAPI schema. Do not edit this section by hand; run `python scripts/generate_lab_tracker_skill_reference.py`.

List/search endpoints use `limit` between 1 and 200 and `offset` of 0 or greater unless an endpoint documents a narrower schema below.

### Request Payloads

#### Projects: `ProjectCreate`
- Required: `name`
- `description` (optional): string | null
- `name` (required): string; min length 1
- `status` (optional): ProjectStatus enum: active, archived | null

#### Questions: `QuestionCreate`
- Required: `project_id`, `text`, `question_type`
- `hypothesis` (optional): string | null
- `parent_question_ids` (optional): list[string(uuid)] | null
- `project_id` (required): string(uuid)
- `question_type` (required): QuestionType enum: descriptive, hypothesis_driven, method_dev, other
- `status` (optional): QuestionStatus enum: staged, active, answered, abandoned, superseded | null
- `text` (required): string; min length 1

#### Notes: `NoteCreate`
- Required: `project_id`, `raw_content`
- `metadata` (optional): object | null
- `project_id` (required): string(uuid)
- `raw_content` (required): string; min length 1
- `status` (optional): NoteStatus enum: staged, committed, archived | null
- `targets` (optional): list[object] | null
- `transcribed_text` (optional): string | null

#### Sessions: `SessionCreate`
- Required: `project_id`, `session_type`
- `primary_question_id` (optional): string(uuid) | null
- `project_id` (required): string(uuid)
- `session_type` (required): SessionType enum: scientific, operational

#### Datasets: `DatasetCreate`
- Required: `project_id`, `primary_question_id`
- `commit_hash` (optional): string | null
- `commit_manifest` (optional): object | null
- `primary_question_id` (required): string(uuid)
- `project_id` (required): string(uuid)
- `secondary_question_ids` (optional): list[string(uuid)] | null
- `status` (optional): DatasetStatus enum: staged, committed, archived | null

#### Analyses: `AnalysisCreate`
- Required: `project_id`, `dataset_ids`, `method_hash`, `code_version`
- `code_version` (required): string; min length 1
- `dataset_ids` (required): list[string(uuid)]
- `environment_hash` (optional): string | null
- `method_hash` (required): string; min length 1
- `project_id` (required): string(uuid)
- `status` (optional): AnalysisStatus enum: staged, committed, archived | null

#### Claims: `ClaimCreate`
- Required: `project_id`, `statement`, `confidence`
- `answers_question_ids` (optional): list[string(uuid)] | null
- `confidence` (required): number; minimum 0.0, maximum 100.0
- `project_id` (required): string(uuid)
- `statement` (required): string; min length 1
- `status` (optional): ClaimStatus enum: proposed, supported, rejected | null
- `supported_by_analysis_ids` (optional): list[string(uuid)] | null
- `supported_by_dataset_ids` (optional): list[string(uuid)] | null

#### Goals: `GoalCreateFields`
- Required: `goal_type`, `title`
- `attributes` (optional): object | null
- `external_ref` (optional): string | null
- `goal_type` (required): GoalType enum: paper, grant, talk, other
- `status` (optional): GoalStatus enum: planned, in_progress, submitted, accepted, abandoned | null
- `summary` (optional): string | null
- `target_date` (optional): string(date) | null
- `title` (required): string; min length 1

#### Visualizations: `VisualizationCreate`
- Required: `analysis_id`, `viz_type`, `file_path`
- `analysis_id` (required): string(uuid)
- `caption` (optional): string | null
- `file_path` (required): string; min length 1
- `related_claim_ids` (optional): list[string(uuid)] | null
- `viz_type` (required): string; min length 1

#### Graph Drafts: `GraphDraftCreateRequest`
- Required: none
- `mode` (optional): GraphDraftMode enum: graph_context, image_only, graph_batch
- `user_hint` (optional): string; min length 1 | null

#### Decision Context: `AssistantDecisionContextRequest`
- Required: `task_kind`, `query`
- `analysis_id` (optional): string(uuid) | null
- `claim_id` (optional): string(uuid) | null
- `dataset_id` (optional): string(uuid) | null
- `limit` (optional): integer; minimum 1.0, maximum 100.0, default 20
- `project_id` (optional): string(uuid) | null
- `query` (required): string; min length 1
- `question_id` (optional): string(uuid) | null
- `task_kind` (required): string; min length 1
- `visualization_id` (optional): string(uuid) | null

### List/Search Query Parameters

#### `GET /projects`
- `status` (optional): ProjectStatus enum: active, archived | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /questions`
- `project_id` (optional): string(uuid) | null
- `status` (optional): QuestionStatus enum: staged, active, answered, abandoned, superseded | null
- `question_type` (optional): QuestionType enum: descriptive, hypothesis_driven, method_dev, other | null
- `search` (optional): string | null
- `q` (optional): string | null
- `parent_question_id` (optional): string(uuid) | null
- `ancestor_question_id` (optional): string(uuid) | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /notes`
- `project_id` (optional): string(uuid) | null
- `status` (optional): NoteStatus enum: staged, committed, archived | null
- `target_entity_type` (optional): EntityType enum: project, question, dataset, note, session, analysis, claim, visualization, goal | null
- `target_entity_id` (optional): string(uuid) | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /sessions`
- `project_id` (optional): string(uuid) | null
- `status` (optional): SessionStatus enum: active, closed | null
- `session_type` (optional): SessionType enum: scientific, operational | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /datasets`
- `project_id` (optional): string(uuid) | null
- `status` (optional): DatasetStatus enum: staged, committed, archived | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /analyses`
- `project_id` (optional): string(uuid) | null
- `dataset_id` (optional): string(uuid) | null
- `question_id` (optional): string(uuid) | null
- `status` (optional): AnalysisStatus enum: staged, committed, archived | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /claims`
- `project_id` (optional): string(uuid) | null
- `status` (optional): ClaimStatus enum: proposed, supported, rejected | null
- `dataset_id` (optional): string(uuid) | null
- `analysis_id` (optional): string(uuid) | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /projects/{project_id}/goals`
- `project_id` (required): string(uuid)
- `goal_type` (optional): GoalType enum: paper, grant, talk, other | null
- `status` (optional): GoalStatus enum: planned, in_progress, submitted, accepted, abandoned | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /visualizations`
- `project_id` (optional): string(uuid) | null
- `analysis_id` (optional): string(uuid) | null
- `claim_id` (optional): string(uuid) | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /graph-drafts`
- `project_id` (optional): string(uuid) | null
- `status` (optional): GraphChangeSetStatus enum: drafting, ready, submitted, changes_requested, rejected, failed, committed | null
- `source_note_id` (optional): string(uuid) | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /search`
- `q` (required): string
- `project_id` (optional): string(uuid) | null
- `goal_id` (optional): string(uuid) | null
- `include` (optional): string | null
- `limit` (optional): integer; default 20; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation
<!-- END GENERATED API REFERENCE -->

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

The retained-v1 runtime is defined by `docs/retained-v1-surface.md`. Deferred
ideas from `idea.md` should not be treated as active product requirements unless
a bead explicitly says to implement them.
