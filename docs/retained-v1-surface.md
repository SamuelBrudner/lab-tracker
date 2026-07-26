# Retained V1 Surface

This document is the single source of truth for the `lab-0rm` cleanup work.
If existing code, UI text, or older docs disagree with this file, this file
defines the supported v1 product surface.

## Decision

The retained v1 product is the smallest workflow set that preserves the core
research record:

- Auth and role-based access control, including project memberships and
  project-group memberships as Lab Tracker-owned semantic access edges.
- Project groups (`kind=lab`) as an optional organizing container above
  projects, with each project belonging to at most one group through
  `projects.group_id`.
- Projects as the durable unit of research work.
- Group owners inheriting owner access on child projects for PI oversight,
  while group viewers and contributors inherit no child-project access unless
  the explicit `group_read_all` flag is enabled for that group; when enabled,
  that inherited access is read-only.
- Questions created, staged, activated, maintained explicitly by users, and
  connected with `parent_question_ids` to form broad-to-atomic hierarchies.
- Simple query/search flows over questions and notes using the built-in
  substring behavior.
- Manual note capture, including text notes, multipart raw file upload, raw file
  download, raw voice notes with editable transcripts, and attaching notes to
  retained entities.
- Consumer-side figure capture through the Python client (`savefig`,
  `capture_figures`, and `run_context`) and MATLAB package
  (`labtracker.savefig` and `labtracker.uploadFigure`) as fail-soft
  staged-note workflows: Lab Tracker stores a bounded review image or pointer
  plus source URI and content-hash metadata, while full figure files remain in
  the consumer repo.
- Consumer-side watch-folder capture through the `lt watch` CLI as an
  offline-first adapter workflow: watched files and workflow-written manifests
  write durable local outbox records that later sync into staged evidence notes
  or retained acquisition-session outputs. Large outputs can remain external
  pointers, acquisition outputs still belong to sessions, and graph meaning
  remains human-gated through normal review.
- Consumer-side HPC analysis capture through the `lt hpc` CLI as an
  offline-first staged-note workflow: Slurm/HPC submit, begin, finish, and
  watch-folder manifest events write durable local outbox records that sync
  compact scheduler facts, git context, metrics, log excerpts, and external
  artifact pointers. Large outputs remain outside Lab Tracker, and any proposed
  analysis/question/claim meaning remains human-gated through graph drafts.
- Consumer-side analysis-repo capture through the `lt repo` CLI as an
  offline-first staged-note workflow: a fail-soft managed post-commit hook,
  explicit reports, and run-finish events record commit state, declared
  artifact pointers (hashes, never bytes), and an environment fingerprint into
  durable local outbox records that sync as `provider=git` staged notes under
  the shared `<normalized-remote>@<commit>` evidence identity. Capture is
  event-based — Lab Tracker never clones or continuously monitors
  repositories — and the staged-note sink works under today's device-token
  allowlist while draft requests need a user or personal-access token. See
  [repo-report-capture.md](repo-report-capture.md).
- Package-pinned code-facing idiom teaching rendered from one generator into
  consent-gated managed agent surfaces, with the advisory
  `lab-tracker://code-conventions` MCP resource treating the package text as
  canonical.
- Inert citation annotation tokens for local provenance hints, including
  Markdown and LaTeX comment forms; UUID-bearing tokens should be stripped
  before external sharing unless the recipient should see Lab Tracker-local
  identifiers.
- Human-gated graph draft review for uploaded image notes, voice-note
  transcripts, photo+voice bundles, and scheduled or user-triggered batches over
  staged notes. Drafting may be note-scoped or batch-scoped, but every proposed
  operation requires human edit/accept/reject before commit through normal API
  validation.
- Per-(project, user) graph-draft batch settings and run history for configured
  cadence, run-now, and run-due drafting over staged notes, with a project-level
  default row and `review_assignee` attribution on scheduled user batches.
- Opt-in, per-user review-ready email cues backed by a transactional delivery
  outbox, retry leases, and signed short-lived links. Email contains no project
  or research content, and links still require normal authentication and
  project authorization. Provider acceptance is recorded separately from inbox
  delivery.
- Durable curation provenance that keeps the committed graph honest about
  itself: each accepted graph-draft operation records how it was accepted
  (`human_selected`, `bulk_accepted`, or `auto_accepted`) plus the accepting
  actor and time, with an explicit `accept-all` action that marks a batch as
  bulk-accepted rather than laundering it as per-operation review. Archiving a
  captured note is a first-class action that names a reason (including
  `archived_unreviewed`), so a skipped review degrades visible coverage rather
  than silent trust. See [curation-states.md](curation-states.md).
- Paired-device enrollment for phone capture, including one-time enrollment
  URLs, device-token capture, and revocation.
- Human-in-browser personal-access-token minting on the Agents page
  (`/app/agents`), including role/read-only level selection capped at the
  issuer's role, one-time secret display with copy-paste `lt setup connect`
  and MCP setup commands, and token listing/revocation. Service principals
  stay blocked from `/auth/*`, and device principals from everything under
  `/auth/*` except read-only `/auth/me` session introspection; see
  [agent-setup.md](agent-setup.md).
- Project graph views and exports for inspecting the retained question,
  evidence, goal, analysis, claim, dataset, session, and visualization graph.
- Sessions and acquisition outputs, including closing sessions and promoting
  eligible sessions into datasets.
- Dataset staging and direct commit with provenance/manifest capture, without
  an approval gate. The direct-commit path for people and the human-gated review
  path for AI proposals are deliberately asymmetric; see
  [review-and-commit-model.md](review-and-commit-model.md).
- Analysis, claim, and visualization records as explicit user-driven flows,
  including managed file storage for visualization assets.
- Exploration nodes for the divergent research trajectory — `decision`,
  `dead_end`, and `pivot` records that each target a retained question,
  dataset, analysis, or claim and link into a DAG through `parent` and
  `also_depends_on` edges. They render in the project graph between claims and
  visualizations and export as `lab:ExplorationNode` PROV-O records. Like other
  graph entities they are created directly today; any future agent-harvested
  nodes stay human-gated through graph-draft review. See
  [ara-exploration-graph-design.md](ara-exploration-graph-design.md).
- A per-project publication-readiness report
  (`GET /projects/{project_id}/publication-readiness`) that scans the retained
  graph for gaps before write-up — supported claims missing dataset/analysis
  evidence or falsification criteria, answered questions without committed
  dataset evidence, and broken external-artifact references.
- Human-gated provenance links over `GET`/`PATCH /provenance-links`. The daily
  batch run deterministically proposes a `was_derived_from` link whenever two
  captured artifacts share a content hash (e.g. an acquisition output reused as
  an analysis input, possibly across machines); a person accepts or rejects each
  one, and only accepted links render as `prov:wasDerivedFrom` in PROV-O export.
  Nothing is auto-committed and there is no machine-driven create path — the
  detector only writes proposals into the existing review gate.
- Bounded recent analysis retrieval through `GET /analyses?recent_first=true`,
  so workspace summaries can load the newest committed analyses without scanning
  a project's full analysis history. The note, session, dataset, analysis,
  claim, and visualization list endpoints accept `created_by` (author) and
  `since`/`until` time-window bounds, so an assistant can pull what a given
  person committed within a window (e.g. a trainee's work since last week's
  meeting, or your own advances and plots since last July). The decision-context
  endpoint exposes the same `created_by`/`since`/`until` filters plus a
  `progress_review` task kind, assembling a person-scoped, windowed briefing in
  one call. The caller supplies the window; Lab Tracker stores no meeting
  schedule.
- Goals and goal links as explicit planning and evidence-spanning records
  connected to retained graph entities.
- PROV-O/JSON-LD provenance export, record export events, and external artifact
  references that preserve semantic edges to outside tools without
  reimplementing their workflows. The `lt export` consumer-side command writes
  these documents as self-contained sidecar files that survive without a running
  instance, optionally co-located next to the data files they describe. See
  [provenance-export.md](provenance-export.md).
- The linked-data surface around those documents: `@id` identifiers minted
  from `LAB_TRACKER_CANONICAL_BASE_URL` when configured, a public `GET /terms`
  vocabulary page (HTML and JSON-LD) generated from the same registry as the
  embedded `@context`, JSON-LD content negotiation on dataset/analysis/claim
  URIs with the canonical URI echoed as `meta.iri` in plain envelopes, and a
  committed worked example under `docs/examples/` guarded by a drift test.
- On-demand resolution of external artifact references (content hash is the
  integrity gate). Local resolution and registered local-store health share one
  immutable operator-root policy parsed from the platform-path-separated
  `LAB_TRACKER_RESOLVER_ALLOWED_ROOTS`; the application runtime denies all local
  roots when it is unset or empty. Local resolution optionally recovers a
  moved/renamed file by its content hash within those roots
  (`LAB_TRACKER_RESOLVER_RECOVERY`, off by default, bounded, read-only).
  Local health is a bounded, isolated, output-free static directory probe. It is
  advisory rather than registration validation or a handle-bound filesystem
  capability; retarget hardening remains `lab-tracker-n5kp.41.6`.
  Registered `git` data stores resolve `path@commit` locators read-only and
  on demand, gated by an operator remote allowlist
  (`LAB_TRACKER_GIT_ALLOWED_REMOTES`, deny-by-default), a protocol allowlist,
  a fetch size cap, and a bounded cache — never by cloning or polling. Rclone
  resolution and store health are likewise gated by one immutable exact
  remote-name policy (`LAB_TRACKER_RCLONE_ALLOWED_REMOTES`, deny-by-default).
  Local, rclone, and Git health commands reuse resolution's bounded
  cross-platform process executor and expose only static adapter-specific
  failures. See
  [external-artifact-resolution-design.md](external-artifact-resolution-design.md).
- Read-only assistant and MCP decision-context endpoints over the retained
  graph. Assistants may inspect context through these surfaces, but retained v1
  does not delegate graph commits to autonomous agents.

Anything not listed above is out of the retained v1 surface and should not
shape the default runtime, supported docs, or simplified architecture.

Some legacy domain types, tables, and test fixtures may still mention deferred
workflows while the hard-delete work is being decomposed. Treat those as
compatibility surfaces for historical data or cleanup staging, not as supported
product capabilities.

## Deferred Workflows

The following workflows are explicitly deferred and are being retired from the
supported product path:

- OCR-based note transcription.
- Automatic audio transcription on every upload. Voice transcription is an
  explicit note-scoped action for user-captured voice notes.
- Automatic question extraction and extraction inbox workflows. Retained graph
  drafting includes note-scoped drafting and human-gated batch drafting over
  user-captured staged notes; it is not a standing system-selected extraction
  inbox, and nothing commits automatically.
- Entity and tag suggestion workflows derived from notes or OCR output.
- Semantic/vector search, embedding providers, and backend-specific relevance
  ranking.
- Dataset review policy, review requests, review queue, and review UI.

Deferred means:

- keep old data readable only as needed for migration or deletion;
- stop treating these workflows as first-class product paths;
- do not preserve their current implementation shape during refactors.

## Cleanup Guardrails

Follow these rules in sibling cleanup work:

- Default runtime behavior should center the retained workflows only.
- Frontend navigation and supported docs should describe manual,
  straightforward flows first.
- Backend refactors should prefer direct repository-backed operations over
  speculative abstractions created for deferred workflows.
- New work should preserve durable data and invariants, not deferred feature
  surfaces.
- Project groups and group memberships are retained semantic edges only. Do not
  turn them into an org chart, HR system, identity-provider admin surface,
  grant-management product, or cross-lab/co-PI hierarchy.
- Build-vs-buy boundaries are recorded in
  [`docs/build-vs-buy-boundaries.md`](build-vs-buy-boundaries.md). Adjacent
  commodity responsibilities should use pointer-not-reimplementation: keep
  external artifact references plus semantic graph edges, not the external
  tool's workflow or UI.

## Build-vs-Buy Boundaries

Retained v1 keeps the current local auth, local storage, dataset manifest, and
analysis record implementations as working defaults, but those are not the
long-term product moat. The long-term ownership boundary is:

- Build and keep in-house: questions, claims, human-gated graph changes, and the
  PROV-O semantic provenance spine.
- Integrate via adapters: dataset/versioning substrates, experiment trackers,
  ELNs, and object storage.
- Offload where practical later: credential/session/device-grant plumbing.
- Defer: semantic/vector search and standing extraction inboxes.

Do not expand retained-v1 features toward becoming an identity provider, object
store, data-versioning system, experiment tracker, or ELN.

## Restoration Ledger

These ideas are worth preserving even though the current implementations are
not:

| Deferred area | Durable idea to preserve | If reintroduced later, use this shape |
| --- | --- | --- |
| OCR on note upload | Image uploads can become more useful if the system can suggest an editable transcript. | Make OCR an explicit assist on a single note or upload action; store the output as editable transcript/provenance, never as a required ingestion step. |
| Automatic question extraction | Notes can seed question creation, especially for meeting captures and whiteboards. | Reintroduce as an on-demand “generate candidate questions from this note” action with per-candidate accept/reject, not as a standing inbox workflow. |
| Entity/tag suggestion workflows | Machine suggestions can help normalize notes without replacing the raw human record. | Attach suggestions as optional annotations with confidence/provenance on one record at a time; never require them for commit or navigation. |
| Semantic/vector search + embeddings | Cross-note and cross-question retrieval is useful once the core record is stable and large enough to search meaningfully. | Start with opt-in indexing behind one operational switch and a clean substring fallback; avoid provider sprawl in the default runtime. |
| Dataset review queue/policy/UI | Some labs may eventually want second-person approval for selected commits. | Layer review as an optional governance feature on top of direct commit, not as the default dataset lifecycle or a prerequisite for provenance capture. |
