# MCP Evidence Authoring Product Spec

## Problem

Lab Tracker's retained v1 surface treats analyses, claims, and visualizations as
first-class research records. The HTTP API already exposes write routes for these
records, and the MCP server exposes read tools for them. However, MCP clients can
only create projects, questions, and notes.

That gap showed up in a basic retrospective workflow:

1. create a project for prior PhD work;
2. populate the project with a question hierarchy;
3. attach claims and evidence, including plots from papers or analyses.

Step 3 currently forces an assistant to either stop, create unstructured notes, or
drop below the MCP contract and call raw HTTP endpoints. That is exactly the kind
of graph-authoring task the MCP server and `lab-tracker` skill should support.

## Goal

Add MCP and skill support for creating the evidence layer of a Lab Tracker graph:
datasets, analyses, claims, visualizations, and targeted notes.

The core user story is:

> As a researcher using an assistant, I can ask it to add claims and evidence to a
> project, and it can create first-class Lab Tracker records through MCP while
> preserving provenance links between questions, datasets, analyses, claims,
> visualizations, and source notes.

## Non-Goals

- Do not add autonomous claim extraction from papers or notes.
- Do not bypass API validation or write directly to the database.
- Do not require uploaded raw data files for literature-retrospective evidence
  records.
- Do not redesign the claim, analysis, or visualization data models in this pass.
- Do not add frontend UI work unless needed for API/MCP test fixtures.

## Current State

Status update: the core evidence-authoring MCP tools described in this spec have
since shipped. Treat the older "add this tool" language below as historical
design notes unless it names a remaining follow-up.

The MCP server exposes:

- reads for projects, questions, notes, sessions, datasets, analyses, claims, and
  visualizations;
- provenance reads for datasets and analyses;
- creates for projects, questions, notes, datasets, analyses, claims,
  visualizations, goals, claim edges, and goal links;
- `lab_tracker_record_evidence_bundle` for the one-result convenience workflow.

The API exposes write routes for:

- `POST /datasets`
- `POST /analyses`
- `POST /claims`
- `POST /visualizations`
- `POST /notes`

The `lab-tracker` skill documents the shipped read and write tools, including the
evidence-map authoring order.

## Product Requirements

### 1. Create Targeted Notes Through MCP

Add `lab_tracker_create_note` support for optional `targets`.

Required shape:

- `project_id`
- `raw_content`
- optional `transcribed_text`
- optional `metadata`
- optional `status`
- optional `targets`: list of `{entity_type, entity_id}`

Rationale: source notes should be attachable to a question, dataset, analysis,
claim, or visualization instead of only to the project.

Acceptance criteria:

- MCP can create a note targeted at a claim.
- MCP can create a note targeted at a visualization.
- Invalid target entity types or IDs return API validation errors.
- Existing note creation behavior remains backward-compatible.

### 2. Create Datasets Through MCP

Add `lab_tracker_create_dataset`.

Required shape should mirror `POST /datasets`:

- `project_id`
- `primary_question_id`
- optional `secondary_question_ids`
- optional `commit_hash`
- optional `commit_manifest`
- optional `status`

Recommended default: `staged`.

For retrospective paper evidence, staged datasets are acceptable placeholders for
source collections such as "PhD dissertation analyses" or "PLoS Comput Biol 2023
juvenile syllable recordings". Committed datasets should still require a valid
manifest and files, following existing API rules.

Acceptance criteria:

- MCP can create a staged dataset linked to a primary question.
- MCP can create a dataset with secondary question links.
- Attempts to commit without files or a valid manifest fail through normal API
  validation.
- The skill clearly explains when staged retrospective datasets are appropriate.

### 3. Create Analyses Through MCP

Add `lab_tracker_create_analysis`.

Required shape should mirror `POST /analyses`:

- `project_id`
- `dataset_ids`
- `method_hash`
- `code_version`
- optional `environment_hash`
- optional `status`

Recommended default: the API default analysis status.

For retrospective authoring, `method_hash` and `code_version` may be stable human
labels when source code hashes are unavailable, for example:

- `method_hash="publication:eLife-2021-vae-feature-space"`
- `code_version="published-pdf:elife-67855-v2"`

Acceptance criteria:

- MCP can create an analysis linked to one or more datasets.
- API validation rejects empty dataset lists and unknown dataset IDs.
- The skill tells agents to prefer real hashes/versions when available and stable
  publication labels only for retrospective evidence.

### 4. Create Claims Through MCP

Add `lab_tracker_create_claim`.

Required shape should mirror `POST /claims`:

- `project_id`
- `statement`
- `confidence`
- optional `status`
- optional `supported_by_dataset_ids`
- optional `supported_by_analysis_ids`
- optional `answers_question_ids`
- optional `external_citations`

Recommended status rules:

- use `supported` only when the claim is linked to a dataset or analysis;
- use `proposed` when the claim is imported from human interpretation without a
  concrete supporting record yet;
- never mark as `supported` to express confidence alone.

Acceptance criteria:

- MCP can create a proposed claim without evidence links.
- MCP can create a supported claim linked to an analysis.
- API validation rejects `supported` claims without a dataset or analysis.
- Skill guidance distinguishes claims from questions and notes.

### 5. Create Visualizations Through MCP

Add `lab_tracker_create_visualization`.

Required shape should mirror `POST /visualizations`:

- `analysis_id`
- `viz_type`
- `file_path`
- optional `caption`
- optional `related_claim_ids`

For plots from papers, `file_path` may be a stable source locator rather than a
local rendered image path, for example:

- `/Users/.../journal.pcbi.1011051.pdf#fig5`
- `doi:10.1371/journal.pcbi.1011051#fig5`

The skill should still prefer real local file paths when an actual plot artifact
exists.

When the local artifact exists, agents should upload it as a managed
visualization file after creating the visualization node:

- `POST /visualizations/{viz_id}/file`
- MCP convenience: `lab_tracker_upload_visualization_file`

The visualization response should expose asset metadata, checksum, and a stable
download path so remote clients are not dependent on the original local
filesystem path.

Acceptance criteria:

- MCP can create a visualization linked to an analysis.
- MCP can link a visualization to one or more claims.
- MCP can upload a local figure file into managed storage for the visualization.
- API clients can download the managed visualization file by visualization ID.
- API validation rejects unknown analysis or claim IDs.
- Skill guidance explains acceptable `file_path` conventions for published
  figures versus generated plot files, and prefers managed uploads when plot
  files exist.

### 6. Shipped Convenience: Record Evidence Bundle

`lab_tracker_record_evidence_bundle` shipped as the one-result convenience tool.
The dedicated `lab_tracker_commit_analysis` name did not ship; use the bundle
tool for the compact dataset -> analysis -> claim -> visualization workflow.

The bundle input covers:

- source note information;
- dataset identity and manifest/hash fields;
- analysis method/code provenance;
- claim text, confidence, and question/support links;
- visualization metadata or managed file upload details.

Rationale: this supports a compact "record the analysis output" workflow after
datasets and analysis are already staged. It should not replace the individual
create tools because agents often need to build evidence maps incrementally.

Acceptance criteria:

- MCP can plan or record a one-result evidence bundle through the strict atomic
  bundle API, with a client-side managed-file upload follow-up when requested.
- Skill guidance recommends individual create tools for incremental graph
  population and the bundle helper for one-shot analysis-output recording.

## Skill Updates

Update `skills/lab-tracker/SKILL.md` and `docs/lab-tracker-mcp-skills.md` with an
"Evidence Authoring" section.

The section should tell agents to:

1. read existing questions, datasets, analyses, claims, visualizations, and notes
   before creating evidence records;
2. create or reuse datasets before creating analyses;
3. create analyses before creating supported claims or visualizations;
4. attach source notes to the most specific relevant entity;
5. use `supported` claim status only with supporting dataset or analysis IDs;
6. use publication locators for retrospective figures only when no local plot file
   exists;
7. verify the final graph with list tools.

It should also state that agents must be explicit before mutating research
records, consistent with the current skill.

## Tool Naming

Add these MCP tools:

- `lab_tracker_create_dataset`
- `lab_tracker_create_analysis`
- `lab_tracker_create_claim`
- `lab_tracker_create_visualization`
- `lab_tracker_upload_visualization_file`
- `lab_tracker_record_evidence_bundle`

Extend:

- `lab_tracker_create_note` with `targets`

Do not overload `lab_tracker_create_note` to represent claims or plots. If a
record is a claim or visualization in the Lab Tracker domain, it should be a
first-class claim or visualization.

## Implementation Notes

- Keep the MCP client as a thin API wrapper; no direct database writes.
- Reuse `_drop_empty` and API error formatting.
- Add local enum validation where the MCP server already has model enum values
  available; otherwise rely on API validation.
- Prefer schema-compatible JSON inputs over strings that require parsing.
- Preserve auth behavior: service account login and one retry on 401.
- Add tests using the same MCP/API-backed patterns already used for project,
  question, and note tools.

## Test Plan

Backend/MCP tests:

- create targeted note with a question target;
- create staged dataset linked to a primary question;
- create analysis linked to dataset;
- create proposed claim with no support;
- create supported claim linked to analysis;
- reject supported claim with no support;
- create visualization linked to analysis and claim;
- reject visualization linked to unknown claim;
- verify list tools return created records.

Skill/doc validation:

- `skills/lab-tracker/SKILL.md` lists the new tools.
- `docs/lab-tracker-mcp-skills.md` lists the new tools.
- Evidence-authoring guidance includes ordering, status rules, and retrospective
  source-locator conventions.

Regression:

- existing MCP read tools still pass;
- existing create project/question/note tools remain backward-compatible;
- `uv run pytest -q`;
- `uv run ruff check .`.

## Open Questions

1. Should retrospective literature evidence use staged datasets, or should Lab
   Tracker add a distinct "source" or "publication" entity later?
2. Should visualization `file_path` accept URI-like values by contract, or should
   source locators live in note metadata until plot files exist?
3. Should claims be linkable directly to questions, or is the current indirect
   route through datasets/analyses sufficient for v1?
4. Should MCP expose update/delete tools for evidence records, or keep this pass
   create/read-only to reduce accidental graph churn?

## Recommended MVP

Ship the first pass as:

1. targeted note creation;
2. create dataset;
3. create analysis;
4. create claim;
5. create visualization;
6. skill/doc updates;
7. tests for the end-to-end evidence-authoring flow.

Leave a dedicated `lab_tracker_commit_analysis` tool, update/delete tools, and
any model changes for follow-up work.
