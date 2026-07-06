# Server-Resident Agentic Drafting: Who Runs the Proposal Agent, and For Whom

Companion to the "Server-resident agentic drafting" epic
(`lab-tracker-1325`). This note settles two coupled questions before any
of it ships: where the agents that write graph proposals run (the server
host), and who a scheduled draft is generated for (a specific user).

## What is and isn't at stake

**Not at stake: autonomy.** Nothing here weakens "AI can suggest; only a
person commits." Every proposed operation still flows through the
existing draft review surface, and the precondition this design set for
itself — a structural, fail-closed human gate in mainline — turned out
to already be satisfied while it was being written (see component 1).

**Mostly not at stake: the retained surface.** `docs/retained-v1-surface.md`
already includes human-gated batch drafting. Components 1, 2, and 4
change who triggers drafting and where the model runs its loop —
reliability and quality changes inside the existing contract. Component
3 does extend one enumerated item: the retained surface names
*per-project* batch settings, and this design makes them
per-(project, user) and adds a designated reviewer, so
`retained-v1-surface.md` and `review-and-commit-model.md` must be
amended when component 3 ships. The autonomy contract is untouched
throughout.

**At stake: reliability, multi-user correctness, and draft quality.**
Three defects motivate the design:

1. **The heartbeat is a client-side liability.** The server deliberately
   runs no scheduler; an external cron/launchd/Task Scheduler job on
   somebody's machine must poll `POST /batches/run-due`. That job was
   never installed on the workstation deployment (`lab-tracker-mt3n`),
   and its absence produces no error — scheduled drafting silently does
   not happen.
2. **Scheduled drafts have no human author.** Review is user-specific:
   accepting, editing, and submitting are all gated on the change set's
   author, with only a global-admin override
   (`_is_graph_change_set_author`, `graph_draft_service.py`). But a
   scheduled batch draft is stamped
   `created_by` from the calling actor — the scheduler's admin
   credential. On an auth-enabled shared host, no ordinary contributor
   can work a scheduled draft; only global admins can. The solo-SQLite
   deployment masks this because every request resolves to the same
   `local-tester` identity.
3. **The single-shot draft call cannot look anything up.** Today's
   proposal writer is one structured-output model call
   (`graph_drafting.py`). It cannot search for existing questions or
   claims before proposing, which is exactly the known
   duplicate-node-proposal risk: good drafting wants list-before-create.

## Current state (verified 2026-07-02)

- The proposal model call already runs server-side, inside the API
  process, with server-held provider keys
  (`docs/analysis-graph-drafts-ci.md`: "The model call happens inside
  the Lab Tracker API process"). Clients only capture evidence and
  trigger drafting over HTTP. The MCP surface has no draft-authoring
  tools.
- The real schedule already lives in the database: per-project
  `next_run_at`, claimed by compare-and-set, with deterministic
  `batch_key` dedupe — built so that dumb concurrent polling is safe.
  A self-ticking server would claim work through the same CAS path
  external pollers exercise, so the same properties should hold, even
  with multiple workers.
- `PrincipalType.SYSTEM` / `system_auth_context()` exists with no
  production callers, and the structural gate excluding it from
  accept/commit is already on mainline (see component 1). It is
  scaffolding for exactly this design.
- Drafting runs synchronously inside HTTP request handlers; `run-due`
  performs N projects × model calls in one request. Tolerable for
  single-shot calls, untenable for agent loops.
- Batch cadence settings are per-project (`graph_draft_batch_settings`,
  unique on `project_id`). There is no per-user settings storage of any
  kind (`UserModel` carries only identity fields — id, username,
  password hash, role, created-at).

## Design

Four components, in dependency order; verification found the first
already satisfied.

### 1. Structural human-commit gate — precondition, already landed (`lab-tracker-1325.1`)

"Automation proposes, humans commit" must be true by construction
before any autonomous agent runs on the host: non-interactive
principals (SYSTEM, SERVICE) structurally cannot accept, bulk-accept,
or commit on any code path, fail-closed at the authorization layer.

Verification during this design pass found the gate **already on
mainline**, landed 2026-07-02 (commits `6889069` "enforce structural
human-commit gate on graph drafts" and `57277e6` "exclude service
tokens from the graph-draft review gate"): `require_interactive`
(`project_authorization.py`) is enforced at accept, bulk-accept, and
commit in the service layer, the reserved `auto_accepted` mode is
rejected at both the service and repository layers, and tests cover the
SYSTEM principal and service tokens. The "unmerged branch" status note
in `docs/vision.md` predated the merge by a day and is amended
alongside this doc. `lab-tracker-1325.1` is closed as
verified-already-done; the precondition is met.

### 2. In-process scheduler tick + job queue and worker (`lab-tracker-1325.2`)

A lifespan asyncio ticker, behind a config flag, fires due batches as
the SYSTEM principal — no external scheduler required. Draft generation
moves off the request thread into a DB-backed job queue worked by an
in-process worker; the job table generalizes the existing
`GraphDraftBatchRun` pattern (status, compare-and-set claims,
`error_metadata`). `POST /batches/run-due` remains for operators who
prefer a cloud scheduler; the installer scripts become a documented
fallback. This would retire the `mt3n` failure class and give the
desktop-launcher topology "open the app and find drafts waiting" with
zero installers.

This amends the letter of the deployment philosophy in `docs/vision.md`
("zero background machinery") while keeping its substance: the database
still does the real scheduling; the process merely gains a timer and a
worker loop that claim work through the same CAS machinery external
pollers use. Amend `vision.md` and `docs/scheduled-daily-review.md` when
this ships.

### 3. User-partitioned batches with a designated reviewer (`lab-tracker-1325.3`)

Scheduled generation is partitioned per **(project, note author)**: a
batch window covers only staged notes captured by that user, `batch_key`
gains the user dimension, and cadence settings become per
(project, user) with a project-level default. Each scheduled draft
carries a new `review_assignee`, and **all** author-keyed review gates —
accept/reject of operations, bulk-accept, edit, submit — re-key from
creator to assignee (concretely: the `_is_graph_change_set_author`
predicate resolves to the assignee on scheduled drafts; re-keying only
edit and submit would leave the assignee unable to accept, reproducing
the defect this component exists to fix). Owner-only commit stays
project-level as the shared governance gate. When this ships, amend
`retained-v1-surface.md` (per-project → per-(project, user) settings)
and `review-and-commit-model.md` (author-keyed review description).

Attribution stays honest, in keeping with the origin-honesty work: the
system generated the draft, so `created_by` remains the triggering
principal (SYSTEM); the assignee is recorded as the designated reviewer,
not faked as the creator. Note-scoped on-demand drafts are unchanged —
there the requester genuinely is the author.

What this gives up: single-batch synthesis across two users' notes in
one window. Acceptable — every operation cites its source notes, and
the agent (component 4) reads shared graph context regardless of whose
notes seeded the batch.

Per-user *notification* preferences (reminder time, live timezone) are
deliberately out of scope here; that is the Morning Read layer
(`lab-tracker-udv1.7`) on top of this partitioning.

### 4. Agentic draft client (`lab-tracker-1325.4`)

Replace the single-shot call with a tool-using agent behind the seam
that already exists: `GraphDraftClient` is a protocol with three
interchangeable provider implementations, injected via
`app.state.graph_draft_client_factory`. An `AgenticGraphDraftClient`
runs an internal read-only tool loop — graph context, substring search
over existing questions/claims, decision context — and terminates in the
same structured graph-patch schema. Provenance stamping
(`origin_provider` / `origin_model` / `origin_prompt_version`) and the
whole human review lifecycle apply unchanged. The agent has no write
path, enforced structurally (read-only tools), not by prompt. It runs
only in the background worker, never in a request handler, and the
single-shot providers remain selectable as a config rollback.

## Rejected alternatives

- **External agent harnesses POSTing pre-made proposal patches** (a new
  proposal-upload endpoint). More general, but it opens a
  proposal-injection surface and gives up server-held keys and
  server-stamped attribution. Revisit only if third-party harnesses must
  someday propose drafts.
- **Per-user cadence over the shared, unpartitioned note stream.** Two
  users with different cadences on one project would produce duplicate
  or fragmented batches over the same notes. Partitioning by note author
  is what makes per-user cadence coherent.
- **Stamping `created_by` with the assignee on scheduled drafts.** The
  assignee didn't create the draft; pretending otherwise would fabricate
  provenance, the same failure mode the `user_revised` origin-honesty
  fix exists to prevent.
- **Sidecar-only scheduler (cron container, Render cron) instead of an
  in-process ticker.** Keeps the app process pure but does nothing for
  the desktop-launcher topology and keeps the "second thing to
  provision" failure mode. The job-queue abstraction still permits
  moving the worker to a sidecar later without changing the model.

## Adjacent tool landscape (considered 2026-07-06)

Six agentic-data-stack tools were evaluated against this design:
PuppyGraph, Upriver, Compass (Dagster Labs), Malloy, fenic (typedef),
and Ascend.io. Facts and URLs are recorded in
`docs/build-vs-buy-boundaries.md` ("Agentic-Data Tool Landscape"). None
changes decisions 1–4: the deployment topology (SQLite solo,
zero-sidecar desktop launcher) excludes every external engine among
them, and the read tools the agent needs are graph-shaped and small.
Three notes worth keeping:

**Convergence validation.** Upriver grounds its enterprise agents in a
continuously maintained cross-stack context graph; Compass's
differentiator over raw text-to-SQL is a human-approved context store
in front of strictly read-only warehouse queries. Both are the
industry-scale shape of components 3–4: a read-only agent grounded in a
curated semantic context, with human-gated approval of what it
produces. Lab Tracker's graph is the context engine and the draft
review surface is the governance loop; this design is on the
convergence path, not off it.

**Malloy Publisher as a source-visible design reference for component
4.** Publisher (MIT, github.com/malloydata/publisher) ships an
agent-facing MCP server; because it is open source, its concrete
choices were read directly (verified in source 2026-07-06) and compared
against the shipped `AgenticGraphDraftClient`
(`graph_drafting.py`, landed 2026-07-03, `lab-tracker-1325.4` closed).
Note the shipped shape: not a live tool loop but a deterministic
read-only *pre-pass* — tokenize staged notes, substring-match against
node summaries already in the batch context, attach a bounded
`agentic_tool_trace` plus a link-before-create hint, then delegate to
the unchanged single-shot structured call.

Where the shipped implementation already matches Publisher's choices:

- *Read-only by construction.* Publisher enforces read-only-ness by
  tool absence plus process isolation (its agent MCP server exposes
  exactly two retrieval tools; query execution lives on a separate
  port). The pre-pass is stronger still: the model gets no live tool
  surface at all.
- *Context budget as a first-class constraint.* Publisher caps results
  (default 10, max 50), truncates doc excerpts to 280 chars, and caps
  queries at 500 chars; the pre-pass caps search terms at 20, matches
  at 20, and decision-context summaries at 10 projects.
- *Lexical baseline first.* Publisher's `getContext` is BM25 (Lunr)
  over flattened model entities — no LLM, no embeddings; the pre-pass
  is likewise pure-lexical substring matching.

Where Publisher is ahead — candidate follow-ups
(`lab-tracker-1325.4` is closed; filed as `lab-tracker-su76`):

- *A checked-in retrieval eval.* Publisher ships a recall@K harness
  with labeled query→expected-entity cases next to the tool, candidly
  documenting the lexical gap ("dep_delay" shares no token with
  "departure delay") as measured future-work motivation. Nothing
  measures whether the pre-pass surfaces the right existing nodes,
  which is the duplicate-avoidance point of component 4.
- *Ranked, field-boosted matching.* Publisher boosts entity names 4x
  over body text; the pre-pass substring-matches unranked JSON
  haystacks, so a label hit and an incidental metadata hit score the
  same.
- *Two-phase retrieval and description-as-prompt.* Broad search then a
  scoped drill-down parameter, and tool descriptions that state the
  grounding purpose ("…instead of guessing"), become relevant if the
  pre-pass ever grows into the live tool loop this design originally
  sketched.

Not a dependency: TypeScript/Node runtime and sidecar server, no
SQLite connector, and analytics-shaped (measures/dimensions) rather
than graph-shaped reads. Revisit only if agents someday need
analytical queries over the graph.

**Revisit triggers for the rest.** fenic (Apache-2.0, embeds
in-process) is the reference target if a batch semantic-extraction or
duplicate-claim pre-pass (embedding/semantic join over staged notes)
ever fronts the agent loop — pre-1.0 from a seed-stage vendor, so not
now. PuppyGraph is the reference target only if graph analytics on
shared-Postgres hosts outgrow recursive CTEs; note its MCP query
surface would function as an external agent harness, adjacent to the
rejected alternative above. Ascend.io is winding down as of mid-2026
despite ~$54M raised — a cautionary citation for keeping external-tool
adapters thin and optional, per `build-vs-buy-boundaries.md`.

## Decisions (resolved 2026-07-02)

1. **Placement:** proposal-writing agents run on the server host, inside
   the app process's background worker (upgradeable to a sidecar worker
   later). Clients capture evidence, configure settings, and review.
2. **Scheduling:** the server self-ticks behind a config flag, acting as
   the SYSTEM principal; `run-due` and external schedulers remain as an
   alternative path.
3. **Scoping:** scheduled batch generation is per (project, note
   author), with per-(project, user) cadence and a `review_assignee`
   who holds the edit/submit gate. Commit remains owner-only.
4. **Proposer:** a tool-using agent behind the `GraphDraftClient`
   protocol replaces the single-shot call as the default drafting path,
   read-only by construction, with single-shot providers as fallback.
5. **Precondition:** the structural fail-closed human-commit gate must
   be in mainline first — verified already satisfied (landed 2026-07-02,
   commits `6889069`/`57277e6`); `lab-tracker-1325.1` records the
   verification and is closed.

These resolutions are reflected in the bd descriptions for
`lab-tracker-1325.1`–`.4`. Related hygiene filed separately:
`lab-tracker-0mbs` (cadence-default drift between migration 0024, code,
and docs).
