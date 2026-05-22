# MCP Decision Context Tooling Product Spec

Status: first implementation slice shipped

Owner: Lab Tracker

Bead: `lab-tracker-adm`

## Summary

Lab Tracker should expose read-only MCP tooling that lets Codex and other
assistant clients consult the backend research graph before making
research-facing decisions. The primary user goal is that assistants use Lab
Tracker as grounding context when they are uncertain about what variables to
plot, what analyses to run, what slides to make, what research text to write,
what claims to emphasize, or what experimental controls to prioritize.

This should be implemented as explicit tooling, not only as prose in
`AGENTS.md`. `AGENTS.md` and skills should define the behavioral policy, while
the MCP server should provide a single high-level decision-context tool backed
by bounded, provenance-preserving graph retrieval.

## Problem

The current MCP surface exposes projects, questions, notes, and search. That is
enough for a determined assistant to inspect some context, but it requires the
assistant to invent a retrieval strategy every time. That produces inconsistent
behavior:

- the assistant may search the wrong terms;
- it may miss datasets, analyses, claims, or visualizations;
- it may choose a plot or manuscript argument from generic reasoning rather than
  the active research graph;
- it may fail quietly when graph context is unavailable;
- it may overfit to recent chat context instead of durable project context.

The product need is a predictable, read-only context packet for assistant
decision-making.

## Goals

- Give assistants one obvious tool to call before research-facing decisions.
- Support task-specific context for plotting, analysis, slides, experiment
  planning, summaries, and research writing.
- Preserve stable IDs and provenance links for every returned entity.
- Prefer explicit graph links, active questions, committed artifacts, and recent
  notes over opaque model retrieval.
- Keep the tool read-only by default. Consultation must not mutate Lab Tracker.
- Fail loudly when the graph cannot be reached or the request is too ambiguous.
- Keep retained-v1 behavior consistent with `docs/retained-v1-surface.md`: use
  explicit links, substring search, hierarchy traversal, and bounded recency
  rather than semantic/vector search.

## Non-Goals

- No autonomous writes, drafts, issue creation, graph mutations, or note creation.
- No automatic background extraction or standing inbox workflow.
- No semantic/vector search requirement in the first implementation.
- No replacement for human review of scientific claims.
- No guarantee that returned context is sufficient for publication; the assistant
  must still identify missing evidence and caveats.

## Users And Workflows

Primary users are researchers using assistant clients to work with Lab Tracker
projects. Initial assistant clients are Codex and Claude-like MCP clients.

The tool should be called before an assistant:

- chooses dependent or independent variables for plots;
- chooses datasets, grouping factors, controls, or statistical models;
- decides what figures, tables, or slides to make;
- writes manuscript, grant, abstract, result, discussion, caption, or talk text;
- summarizes the state of a project or question;
- recommends experimental controls, follow-up experiments, or analysis branches.

The tool does not need to be called for pure software maintenance, formatting,
dependency updates, or non-research code refactors unless the user asks for
research content or the work changes research semantics.

## Task Kinds

The first supported `task_kind` values are:

| Value | Use When | Context Emphasis |
| --- | --- | --- |
| `plot` | Choosing plots, axes, variables, groupings, panels, or figure candidates. | Relevant datasets, question links, prior analyses, visualizations, notes naming measured variables, caveats. |
| `analysis` | Choosing analyses, models, controls, comparisons, or validation checks. | Active questions, dataset provenance, method hashes, prior analyses, supported and rejected claims, confounds. |
| `slides` | Choosing talk structure, slide sequence, visual evidence, or speaker narrative. | High-level motivating questions, strongest claims, key visualizations, caveats, unresolved questions. |
| `experiment_plan` | Choosing experimental controls, conditions, protocols, or next measurements. | Active method-development questions, notes, sessions, datasets, open caveats, unresolved controls. |
| `summary` | Summarizing project or question state. | Project hierarchy, active and answered questions, recent notes, datasets, analyses, claims, visualizations. |
| `research_writing` | Writing manuscripts, grants, abstracts, result sections, discussion text, figure legends, paper outlines, or talk prose. | Evidence map from questions to claims to datasets/analyses/visualizations, unsupported claims, caveats, missing controls, suggested figures and tables. |

Future task kinds may be added, but assistants should treat unknown values as
errors rather than silently falling back to generic context.

## MCP Tool Surface

### `lab_tracker_get_decision_context`

Primary read-only tool. This is the tool assistant policies should name.

Request shape:

```json
{
  "task_kind": "research_writing",
  "query": "Draft a results narrative for CO2 priming effects on optogenetic behavior.",
  "project_id": "optional UUID",
  "question_id": "optional UUID",
  "dataset_id": "optional UUID",
  "analysis_id": "optional UUID",
  "claim_id": "optional UUID",
  "visualization_id": "optional UUID",
  "limit": 20
}
```

Required fields:

- `task_kind`: one of the supported task kinds.
- `query`: plain-language user intent. Empty queries are invalid.

Optional anchors:

- `project_id`
- `question_id`
- `dataset_id`
- `analysis_id`
- `claim_id`
- `visualization_id`

Anchors constrain and prioritize context. If anchors conflict, the tool should
return a structured ambiguity error rather than blending unrelated projects.

Response shape:

```json
{
  "data": {
    "task_kind": "research_writing",
    "query": "Draft a results narrative for CO2 priming effects on optogenetic behavior.",
    "generated_at": "2026-05-22T19:30:00Z",
    "scope": {
      "project": {
        "project_id": "uuid",
        "name": "Mosquito Optogenetics",
        "status": "active"
      },
      "anchors": [
        {
          "entity_type": "question",
          "entity_id": "uuid",
          "label": "How does CO2-dependent behavioral state change pathway-specific behavioral responses?"
        }
      ]
    },
    "context_summary": "Short assistant-readable summary of the graph slice.",
    "task_guidance": {
      "recommended_focus": [],
      "candidate_outputs": [],
      "caveats": [],
      "missing_evidence": []
    },
    "questions": [],
    "notes": [],
    "sessions": [],
    "datasets": [],
    "analyses": [],
    "claims": [],
    "visualizations": [],
    "evidence_map": [],
    "truncation": {
      "was_truncated": false,
      "sections": []
    }
  },
  "meta": {
    "retrieval_policy": "explicit_links_then_search_then_recency",
    "limit": 20
  }
}
```

Every returned entity must include:

- stable entity type and ID;
- project ID;
- short label or summary;
- status;
- timestamps when available;
- relevance reason, such as `anchor`, `parent_question`, `child_question`,
  `linked_dataset`, `search_match`, or `recent_activity`.

### Low-Level Read Tools

The high-level tool should not be the only way to inspect the graph. The MCP
server should also expose retained-v1 read surfaces that currently exist in the
HTTP API but are missing from MCP:

- `lab_tracker_list_datasets`
- `lab_tracker_list_analyses`
- `lab_tracker_list_claims`
- `lab_tracker_list_visualizations`
- `lab_tracker_get_dataset_provenance`
- `lab_tracker_get_analysis_provenance`

These tools should mirror the API filters, use the same envelopes, and remain
read-only.

### MCP Resources

Add a resource:

```text
lab-tracker://agent-consultation-policy
```

The resource should contain the current assistant policy text for graph
consultation, including task kinds and failure behavior. This gives MCP clients
a discoverable policy even when their local `AGENTS.md` is missing or stale.

## Retrieval Policy

The decision-context tool should use a deterministic retrieval policy.

1. Validate `task_kind`, `query`, pagination, and anchors.
2. Resolve project scope.
   - If `project_id` is supplied, use that project.
   - If only an entity anchor is supplied, infer its project and validate all
     other anchors share it.
   - If no project is supplied, search active projects and questions. If multiple
     projects plausibly match, return ambiguity metadata with candidate projects.
3. Retrieve anchor neighborhoods.
   - For question anchors, include parent questions, child questions, notes
     targeted to the question, datasets linked to the question, analyses linked
     through those datasets or question filters, related claims, and related
     visualizations.
   - For dataset anchors, include primary and secondary question links, commit
     manifest summary, notes, analyses, claims, and visualizations.
   - For analysis anchors, include datasets, linked questions, claims,
     visualizations, and provenance.
   - For claim and visualization anchors, walk backward to analyses, datasets,
     and questions.
4. Search retained text surfaces using the query.
   - Use substring search across questions and notes.
   - Include matching questions and notes with match snippets.
   - Avoid semantic/vector ranking in the first implementation.
5. Add bounded recency fallback.
   - Include recent active or staged questions, recent notes, recent sessions,
     recent datasets, recent analyses, recent claims, and recent visualizations
     within the resolved project.
6. Rank context by priority.
   - Anchors and explicit graph links outrank search matches.
   - Search matches outrank recency.
   - Active/staged questions outrank archived or abandoned entities unless the
     archived entity is an explicit anchor.
   - Committed datasets and analyses outrank staged records for evidence claims.
7. Bound output.
   - Default `limit` should cap each major section.
   - Return totals and truncation metadata when more items exist.
   - Prefer compact summaries over full raw note content when the result would be
     too large.

## Task-Specific Guidance

The tool should return `task_guidance` tuned to `task_kind`.

For `plot`:

- candidate variables or signals named in notes, manifests, analyses, or claims;
- candidate grouping factors, conditions, controls, and time windows;
- datasets that plausibly contain the variables;
- existing visualizations that should be reused or avoided;
- caveats such as missing controls, confounds, or low-confidence claims.

For `analysis`:

- active questions and hypotheses the analysis should address;
- candidate datasets and inclusion or exclusion considerations;
- prior analyses and method hashes to avoid duplication;
- claims that need support, refutation, or quantification;
- recommended controls, sensitivity checks, and negative controls.

For `slides`:

- suggested narrative arc from motivating question to evidence to caveats;
- strongest supported claims;
- visualizations or datasets suitable for slides;
- unresolved questions that should remain explicit;
- slide candidates with provenance IDs.

For `experiment_plan`:

- active method-development and control questions;
- relevant recent sessions and notes;
- datasets or analyses showing gaps;
- candidate experimental conditions and controls;
- risks and decision points.

For `summary`:

- current project state;
- active, answered, abandoned, and superseded question highlights;
- recent activity;
- evidence map;
- unresolved or ambiguous points.

For `research_writing`:

- thesis-level motivating question;
- claim inventory grouped by support status;
- evidence map linking claims to datasets, analyses, and visualizations;
- caveats and missing evidence;
- suggested figure/table candidates;
- statements the assistant should not make without more evidence;
- provenance IDs that can be cited in internal drafts.

## Error And Ambiguity Behavior

The tool should return structured errors that assistant clients can act on:

| Case | Behavior |
| --- | --- |
| API unreachable or readiness failure | Return `unavailable` with readiness details. Assistant must state Lab Tracker was unavailable before proceeding. |
| Invalid task kind | Return `invalid_task_kind` with allowed values. |
| Empty query | Return `invalid_query`. |
| Multiple plausible projects | Return `ambiguous_project` with candidate projects and reasons. |
| Anchor not found | Return `anchor_not_found`. |
| Anchors cross projects | Return `conflicting_anchors`. |
| No direct matches | Return an empty direct-match section plus bounded project recency if a project is known. |

Assistant policy should be:

- If the tool returns context, use it and cite stable IDs when making research
  decisions.
- If the tool returns ambiguity, ask the user to choose a project or anchor.
- If the tool is unavailable, say so explicitly and list assumptions before
  proceeding.

## Agent Policy Integration

Add a concise rule to `AGENTS.md`:

```md
## Lab Tracker Knowledge Graph Consultation

Before research-facing decisions, consult the Lab Tracker MCP server. This
includes choosing variables to plot, analyses to run, figures or slides to make,
experimental controls to prioritize, summaries to write, and research writing
such as manuscripts, grants, abstracts, results, discussion text, and figure
legends.

Prefer `lab_tracker_get_decision_context` when available. Otherwise use
`lab_tracker_list_projects`, `lab_tracker_search`, `lab_tracker_list_questions`,
`lab_tracker_list_notes`, and the low-level dataset, analysis, claim, and
visualization read tools.

If Lab Tracker is unavailable or ambiguous, state that explicitly. Do not create
or mutate Lab Tracker records unless the user explicitly asks.
```

Update `skills/lab-tracker/SKILL.md` with the same policy after the tool ships.

## Security And Permissions

- The decision-context tool must be read-only.
- Use the existing API-backed MCP client path and service-account auth.
- Prefer a read-only service account role when roles support it.
- Do not return raw dataset file contents.
- Do not return full raw assets by default.
- Truncate long note content and include note IDs so the assistant can request
  narrower context if needed.
- Preserve existing auth behavior: username and password are only required when
  `LAB_TRACKER_AUTH_ENABLED=true`.

## Implementation Shape

Recommended implementation sequence:

1. Extract reusable graph context compaction helpers from the graph-draft service
   or create a new context service that can be used by both graph drafting and
   decision context.
2. Add a read-only API endpoint:

   ```text
   POST /assistant/decision-context
   ```

   The MCP server should call this endpoint rather than reading the database
   directly.
3. Add MCP client methods and tools for:
   - `lab_tracker_get_decision_context`
   - low-level retained graph read tools
   - `lab-tracker://agent-consultation-policy`
4. Add tests for:
   - task-kind validation;
   - project and anchor resolution;
   - graph-neighborhood retrieval;
   - truncation metadata;
   - MCP tool registration;
   - read-only behavior;
   - unavailable and ambiguous responses.
5. Update docs, `AGENTS.md`, and `skills/lab-tracker/SKILL.md`.

## Acceptance Criteria

- MCP exposes `lab_tracker_get_decision_context` and documents all supported
  `task_kind` values.
- The tool returns projects, questions, notes, sessions, datasets, analyses,
  claims, visualizations, and an evidence map when relevant data exists.
- The tool supports `research_writing` and returns claim/evidence/caveat guidance
  suitable for manuscripts, grants, abstracts, figure legends, and talk prose.
- The response includes stable IDs and relevance reasons for returned entities.
- The tool is read-only and does not create notes, questions, graph drafts, or
  other records.
- The tool handles unavailable, ambiguous, invalid, and no-match cases with
  structured responses.
- MCP also exposes retained-v1 low-level read tools for datasets, analyses,
  claims, visualizations, dataset provenance, and analysis provenance.
- `AGENTS.md` and the Lab Tracker skill instruct assistants to use the decision
  context tool before research-facing decisions.
- Automated tests cover API context building and MCP registration.

## Open Product Questions

- Should consultation events be logged as explicit read-only audit records, or
  should phase 1 remain stateless?
- Should the tool return full note text for small results, or always compact note
  summaries with IDs?
- Should project ambiguity ask the assistant to pause for the user, or return
  multiple project packets when the result fits within bounds?
- What is the smallest useful default limit for each section before context gets
  too noisy for assistant clients?
