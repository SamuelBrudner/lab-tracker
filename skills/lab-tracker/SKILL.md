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

<!-- BEGIN GENERATED MCP TOOL LIST -->
Use these tools when available. This list is generated from `lab_tracker.mcp_tools.READ_TOOLS` and `WRITE_TOOLS`; do not edit it by hand.

Read tools:
- `lab_tracker_health`: Check Lab Tracker API health; fail softly if the service is unavailable.
- `lab_tracker_readiness`: Check Lab Tracker database and storage readiness.
- `lab_tracker_describe_schema`: Describe fields/enums before create_* calls; use after context lookup.
- `lab_tracker_list_projects`: List visible projects when scoping a follow-up Lab Tracker read.
- `lab_tracker_list_questions`: List/search questions when inspecting known project/question scope.
- `lab_tracker_list_question_refactors`: List refactor history where a question is the source or replacement.
- `lab_tracker_list_notes`: List notes for known scope; use decision context first for research choices.
- `lab_tracker_search`: Search questions and notes when the project or anchor IDs are not known.
- `lab_tracker_list_sessions`: List acquisition/experiment sessions for a known project scope.
- `lab_tracker_list_datasets`: List datasets; create-order is dataset -> analysis -> claim -> visualization.
- `lab_tracker_list_analyses`: List analyses; use after datasets and before claims/visualizations.
- `lab_tracker_list_claims`: List claims for known evidence; claims come after datasets and analyses.
- `lab_tracker_list_claim_edges`: List typed outgoing logic edges for a claim.
- `lab_tracker_list_visualizations`: List visualizations after resolving related analyses or claims.
- `lab_tracker_list_goals`: List goals/outputs when deciding what research objective to advance.
- `lab_tracker_get_goal`: Get one goal with node links before advancing or updating it.
- `lab_tracker_publication_readiness`: Check ARA-Seal L1 structural readiness for one project.
- `lab_tracker_list_node_goals`: List goals linked to one project graph node.
- `lab_tracker_get_dataset_provenance`: Get dataset provenance JSON-LD before reusing evidence.
- `lab_tracker_get_analysis_provenance`: Get analysis provenance JSON-LD before reusing derived evidence.
- `lab_tracker_get_claim_provenance`: Get claim-centric provenance JSON-LD with analysis/dataset/question ancestry.
- `lab_tracker_export_goal_artifact`: Compile a goal into an Ara artifact; pass layer logic/src/trace/evidence for one layer.
- `lab_tracker_export_question_subtree`: Compile a question subtree into layered Ara JSON-LD.
- `lab_tracker_get_decision_context`: CALL THIS FIRST before research-facing decisions.
- `lab_tracker_next_questions`: Rank open active/staged questions on planned/in-progress goals.

Write tools:
- `lab_tracker_create_project`: Create a project only when the user explicitly asks for a new scope.
- `lab_tracker_create_question`: Create a question after project/goal scope is known.
- `lab_tracker_refactor_question`: Supersede a question with a replacement and optional child/note moves.
- `lab_tracker_create_note`: Create a text note when the user asks to record source context.
- `lab_tracker_create_dataset`: Create a dataset before analyses, claims, and visualizations.
- `lab_tracker_create_analysis`: Create an analysis after datasets and before claims or figures.
- `lab_tracker_create_claim`: Create a claim after linking supporting datasets or analyses.
- `lab_tracker_create_claim_edge`: Create a typed claim-to-claim logic edge such as refutes or extends.
- `lab_tracker_create_visualization`: Register a visualization after its analysis and related claims exist.
- `lab_tracker_create_goal`: Create a goal/output before linking questions, datasets, or claims.
- `lab_tracker_update_goal`: Update a Lab Tracker goal/output.
- `lab_tracker_link_node_to_goal`: Tag an existing graph node in relation to a goal/output.
- `lab_tracker_upload_visualization_file`: Upload a local file into managed storage for a visualization node.
- `lab_tracker_record_evidence_bundle`: Defaults to dry-run; pass dry_run=false to write an evidence bundle.
<!-- END GENERATED MCP TOOL LIST -->

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
explicitly bound for LAN serving. Use `docs/lan-shared-graph.md` for the
current serving modes.

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
- `group_id` (optional): string(uuid) | null
- `name` (required): string; min length 1
- `status` (optional): ProjectStatus enum: active, archived | null

#### Questions: `QuestionCreate`
- Required: `project_id`, `text`, `question_type`
- `hypothesis` (optional): string | null
- `parent_question_ids` (optional): list[string(uuid)] | null
- `project_id` (required): string(uuid)
- `question_type` (required): QuestionType enum: descriptive, hypothesis_driven, method_dev, other
- `status` (optional): QuestionStatus enum: staged, active, answered, abandoned, superseded | null
- `terminal_reason` (optional): string; min length 1 | null
- `text` (required): string; min length 1

#### Notes: `NoteCreate`
- Required: `project_id`, `raw_content`
- `client_capture_id` (optional): string | null
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
- `terminal_reason` (optional): string; min length 1 | null

#### Analyses: `AnalysisCreate`
- Required: `project_id`, `dataset_ids`, `method_hash`, `code_version`
- `code_version` (required): string; min length 1
- `dataset_ids` (required): list[string(uuid)]
- `environment_hash` (optional): string | null
- `external_artifacts` (optional): list[object] | null
- `method_hash` (required): string; min length 1
- `project_id` (required): string(uuid)
- `status` (optional): AnalysisStatus enum: staged, committed, archived | null
- `terminal_reason` (optional): string; min length 1 | null

#### Claims: `ClaimCreate`
- Required: `project_id`, `statement`, `confidence`
- `answers_question_ids` (optional): list[string(uuid)] | null
- `confidence` (required): number; minimum 0.0, maximum 100.0
- `external_citations` (optional): list[object] | null
- `falsification_criteria` (optional): string; min length 1 | null
- `project_id` (required): string(uuid)
- `refuting_outcome` (optional): string; min length 1 | null
- `statement` (required): string; min length 1
- `status` (optional): ClaimStatus enum: proposed, testing, supported, rejected | null
- `supported_by_analysis_ids` (optional): list[string(uuid)] | null
- `supported_by_dataset_ids` (optional): list[string(uuid)] | null
- `terminal_reason` (optional): string; min length 1 | null
- `verification_plan` (optional): string; min length 1 | null

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
- `created_by` (optional): string(uuid) | null
- `dataset_id` (optional): string(uuid) | null
- `limit` (optional): integer; minimum 1.0, maximum 100.0, default 20
- `project_id` (optional): string(uuid) | null
- `query` (required): string; min length 1
- `question_id` (optional): string(uuid) | null
- `since` (optional): string(date-time) | null
- `task_kind` (required): string; min length 1
- `until` (optional): string(date-time) | null
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
- `created_by` (optional): string | null
- `parent_question_id` (optional): string(uuid) | null
- `ancestor_question_id` (optional): string(uuid) | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /notes`
- `project_id` (optional): string(uuid) | null
- `status` (optional): NoteStatus enum: staged, committed, archived | null
- `created_by` (optional): string | null
- `since` (optional): string(date-time) | null
- `until` (optional): string(date-time) | null
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
- `created_by` (optional): string | null
- `since` (optional): string(date-time) | null
- `until` (optional): string(date-time) | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /analyses`
- `project_id` (optional): string(uuid) | null
- `dataset_id` (optional): string(uuid) | null
- `question_id` (optional): string(uuid) | null
- `status` (optional): AnalysisStatus enum: staged, committed, archived | null
- `created_by` (optional): string | null
- `since` (optional): string(date-time) | null
- `until` (optional): string(date-time) | null
- `recent_first` (optional): boolean; default False
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /claims`
- `project_id` (optional): string(uuid) | null
- `status` (optional): ClaimStatus enum: proposed, testing, supported, rejected | null
- `dataset_id` (optional): string(uuid) | null
- `analysis_id` (optional): string(uuid) | null
- `created_by` (optional): string | null
- `since` (optional): string(date-time) | null
- `until` (optional): string(date-time) | null
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
- `created_by` (optional): string | null
- `since` (optional): string(date-time) | null
- `until` (optional): string(date-time) | null
- `recent_first` (optional): boolean; default False
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /graph-drafts`
- `project_id` (optional): string(uuid) | null
- `status` (optional): GraphChangeSetStatus enum: drafting, ready, submitted, changes_requested, committing, rejected, failed, committed | null
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
