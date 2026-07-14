# Agentic Live Read Tools: Letting the Proposal Agent Look Things Up, Safely

Companion to the "Agentic graph-draft loop: live scoped repository read
tools" epic (`lab-tracker-2k8j`), which extends the read-only agentic
draft client shipped in `lab-tracker-1325.4`. This note settles how the
graph-proposal agent gains *live* read access to the graph without
becoming a cross-project, sensitivity-blind, filesystem-reading egress
hole. The loop is the easy part; scoping it is the design.

## What is and isn't at stake

**Not at stake: autonomy.** Nothing here weakens "AI can suggest; only a
person commits." Every proposed operation still flows through the draft
review surface, and the fail-closed human-commit gate
(`require_interactive`, verified landed under `lab-tracker-1325.1`)
already excludes SYSTEM/SERVICE principals from accept, bulk-accept, and
commit. Live reads widen only what the agent can *see* before proposing,
never what it can *write*.

**Not at stake: where the agent runs.** The proposal model call already
runs server-side in the background worker with server-held keys
(`docs/server-resident-agentic-drafting-design.md`, component 4). The
agentic client keeps `requires_background_worker = True` and stays
behind `_ensure_draft_client_allowed_here`; a multi-turn tool loop is
untenable on a request thread and never runs there.

**At stake: read-scope correctness, confidentiality, and egress.** Today
the agent cannot look anything up: `AgenticGraphDraftClient.draft_from_batch`
(`graph_drafting.py`) synthesizes a *simulated* read-only tool trace with
pure Python over the already-assembled `batch_context`
(`_agentic_read_only_tool_trace`), injects it into the user hint, and
makes one structured-output call. `READ_ONLY_AGENT_TOOLS` names three
intended tools that no model ever actually invokes. Giving the model the
real read surface exposes three defects that must be fixed *first*:

1. **The worker reads as admin.** The background actor is
   `system_auth_context()` — `PrincipalType.SYSTEM` with `Role.ADMIN`.
   `ProjectAuthorizationPolicy.accessible_project_ids(actor)` returns
   `None` for admins, the wildcard meaning *all projects*
   (`project_authorization.py`). Any live read tool called under this
   identity returns every project's data, defeating per-project
   isolation on a shared host.
2. **Sensitivity suppression is effectively unimplemented for reads.**
   The note-level `sensitivity` tag (`is_sensitive_note`,
   `services/shared.py`) is honored in exactly one place —
   `list_ready_editions`, which blanks the count in the off-app cue. No
   read path (`list_notes`, `search`, `get_decision_context`) nor the
   batch context builder consults it, so a sensitive note's full body
   flows to any tool that touches it.
3. **`resolve_artifact` is an egress surface.** It dereferences external
   artifact pointers — filesystem paths, git remotes, URLs — to real
   content bytes (`mcp_tools/read.py`, `external_artifacts.py`).
   Project-read is enforced, but in an unattended loop it becomes a
   file/remote read governed only by server `allowed_remotes` / local-root
   config. It has no place in an autonomous read set.

## Current state (verified 2026-07-07)

- The "agentic" provider is a simulated single pass, not a loop. The
  three named tools (`inspect_graph_context`,
  `search_existing_graph_nodes`, `summarize_decision_context`) are pure
  functions over the packet; `write_tools_available` is a dict field,
  not an enforced policy on a model.
- There are 26 registered read tools (`READ_TOOLS`, `mcp_tools/read.py`),
  each a plain Python callable that internally issues an HTTP request via
  a process-global, env-authenticated `LabTrackerAPIClient` singleton
  (`client_from_env`). One identity serves every call; the server then
  applies that identity's `accessible_project_ids`.
- The scope primitive already exists and is reusable:
  `RepositoryDecisionContextReader(accessible_project_ids=...)` gates
  every lookup through `_project_allowed` (`decision_context_query.py`).
  `get_decision_context` is the one read tool already built this way.
- Batches are hard single-project (`create_batch_graph_draft` rejects
  multi-project) and carry `review_assignee_user_id`, the natural
  per-run user key. `created_by` stays SYSTEM for honest attribution.
- The base provider clients hold **no** repository access — they only
  build HTTP requests to the model vendor. OpenAI uses the Responses API
  with strict `json_schema` (inlined across three methods, no shared
  helper); Anthropic funnels all drafting through the single
  `_messages_graph_patch` chokepoint with prompt-coaxed JSON; Google
  funnels through `_generate_graph_patch`.
- `draft_from_batch` returns only the patch; the simulated trace is built,
  injected, and discarded. `GraphChangeSet.context_packet` and
  `error_metadata` are JSON columns; `GraphDraftBatchRun` has only
  `error_metadata`. The batch call is retried up to three times and
  breaks on first success.

## Design

Four components in dependency order, plus two product decisions that gate
the first and third. The center of gravity is the executor (component 1);
the loop (component 2) is small once the executor exists.

### 1. Scoped read-tool executor — the safety core (`lab-tracker-2k8j.3`)

A new **service-injected** executor is the single chokepoint for every
model tool call. Because the base provider clients have no repository
access, the executor is built per run by the service (which does) and
passed into the loop. It enforces three properties by construction, not
by prompt:

- **Allowlist, not "all read tools."** An explicit constant admits only
  `get_decision_context`, `search`, the `list_*` family, `get_goal`,
  `next_questions`, `list_question_refactors`, `list_claim_edges`,
  `list_node_goals`, `publication_readiness`, and `describe_schema`, with
  the provenance/export tools gated. No write tool and **no
  `resolve_artifact`** — the read/write separation the codebase already
  makes structural (`write_tools_available: False`) extended to the read
  side.
- **Per-(project, user) scope, never SYSTEM/admin.** The executor reads
  through an `accessible_project_ids`-gated reader mirroring
  `RepositoryDecisionContextReader`, confined to `run.project_id`
  intersected with the resolved target user's project set. Foreign
  `project_id` parameters are rejected; tool `project_id` defaults to
  `run.project_id`.
- **Sensitivity gate.** Per decision `lab-tracker-2k8j.1`, sensitive-note
  content is suppressed at the executor and in the batch context builder
  (`graph_draft_context.py` `_compact_note`, `_source_artifact_packet`) —
  the gate that does not exist today.

Recommended in-process over minting a per-run read-only LPAT: it reuses
the existing scoped-reader pattern and avoids putting a live user
credential in the worker. The executor is independently unit-testable
with no model in the loop.

### 2. Anthropic tool-use loop + (patch, trace) contract (`lab-tracker-2k8j.4`)

Turn the single Anthropic POST (`_messages_graph_patch`, the one
chokepoint) into a bounded multi-turn loop: send `tools`, loop while
`stop_reason == "tool_use"` appending `tool_result` turns computed by the
executor, reuse `self._client` through `_post_provider_request` (so the
test transport and error mapping keep working), and terminate in the
existing `graph_patch_response_schema`. Tool **outputs** — search hits,
node summaries — are wrapped in the same `<untrusted_*>` framing as
source artifacts; they are equally untrusted.

Anthropic goes first: one chokepoint covers note/batch/analysis, and
there is no native `json_schema` to reconcile with function-calling.
`draft_from_batch` changes to return `(patch, trace)` so the service can
persist the trace of the *surviving* attempt (the retry loop breaks on
first success). The trace is a **bounded digest** — tool names, args, and
result ids, never full bodies — stored into `change_set.context_packet`
on success and folded into `error_metadata` on failure. No migration is
needed for change-set-level storage.

### 3. Config flags + worker/service wiring (`lab-tracker-2k8j.5`)

Add, beside the existing `graph_draft_*` settings:
`graph_draft_agentic_tool_loop_enabled` (bool, default `false` — the
master rollback to the simulated pass), `graph_draft_agentic_max_tool_calls`
(int, positive-validated — bounds round-trips against the per-provider
timeout), `graph_draft_agentic_base_provider` (str, default `openai` —
`from_settings` currently hardcodes OpenAI), and
`graph_draft_agentic_sensitivity_policy` (enum, per `lab-tracker-2k8j.1`).
Wire `execute_graph_draft_batch_run` to resolve the target user (per
`lab-tracker-2k8j.2`), build the scoped executor, and inject it into the
loop. Reconcile the loop's own iteration budget with the surrounding
`max_attempts` retry so a tool-heavy attempt is not multiplied blindly.

### 4. Tests + security review (`lab-tracker-2k8j.6`)

Scoped-executor unit tests are load-bearing: user A cannot read project
B; a sensitive note is handled per policy; a foreign `project_id` is
rejected; `resolve_artifact` and all write tools are absent from the
registry. A scripted multi-turn transcript (MockTransport request-count
stepping, or an extended `FakeBatchDraftClient`) exercises the loop
deterministically. Two pre-existing gaps close here: no test asserts
`make_graph_draft_client("agentic")` returns the client, and none
exercises the worker-only fence. Then `/security-review` runs against a
fixed checklist — cross-project isolation, sensitivity redaction, no
artifact-deref, trace excludes sensitive bodies, fail-closed on an
unresolvable user — and the `graph_draft_agentic_tool_loop_enabled` flag
does not flip until it passes.

### Follow-ups (`lab-tracker-2k8j.7`)

OpenAI (extract a shared `_responses_graph_patch` first; reconcile
Responses function-calls with strict `json_schema`) and Google (mind the
`response_mime_type` + tools conflict) tool loops. Per-operation PROV-O
honesty: an `origin_agent_tools` disclosure in `_graph_draft_origin_kwargs`
(`graph_draft_applier.py`) mirroring `origin_provider` across the entity
models, with an Alembic migration (single-head rule). The change-set-level
trace reference from component 2 is the interim; this is the durable
per-operation `prov:used`.

## Product decisions

1. **Sensitivity policy for model-facing reads (`lab-tracker-2k8j.1`).**
   For a note tagged sensitive, does the drafting model get: the note
   omitted entirely, the body redacted but its existence kept as a
   coverage gap (recommended — mirrors the `context_summary` gap-warning
   philosophy), or the content allowed behind a flag? Sets the default of
   `graph_draft_agentic_sensitivity_policy`.
2. **Reviewer-ambiguity fallback identity (`lab-tracker-2k8j.2`) - resolved.**
   Scheduled agentic/live-read and external-harness drafts fail closed without a
   concrete `review_assignee_user_id`. The service refuses to inject scoped read
   tools rather than falling back to the batch-settings owner, project owner, or
   scheduler/admin actor.

## Rejected alternatives

- **Expose all 26 read tools under the env/SYSTEM principal.** The path
  of least resistance and the most dangerous: `accessible_project_ids`
  is `None` for admin, so every tool returns every project. Cross-project
  leakage collapses to this single decision; the executor exists to
  prevent it.
- **Per-run read-only LPAT + HTTP tools instead of an in-process scoped
  reader.** Reuses the already-audited route enforcement, but introduces
  a live user credential in the background worker plus a token issuance
  and revocation lifecycle. Deferred; revisit if in-process scoping
  proves insufficient.
- **Include `resolve_artifact` with an egress allowlist.** Possible, but
  it turns the proposal agent into a file/remote reader for marginal
  drafting benefit. Reserve artifact dereferencing for human-initiated
  resolution.
- **Persist the full tool transcript verbatim for provenance.** Bloats
  rows and re-leaks source-note content into admin-visible run records.
  A bounded digest plus (later) per-operation `origin_agent_tools`
  disclosure keeps provenance honest without the bulk.
