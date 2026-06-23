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
  `capture_figures`, and `run_context`) as a fail-soft staged-note workflow:
  Lab Tracker stores a bounded review image or pointer plus source URI and
  content-hash metadata, while full figure files remain in the consumer repo.
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
- Per-project graph-draft batch settings and run history for configured
  cadence, run-now, and run-due drafting over staged notes.
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
- Project graph views and exports for inspecting the retained question,
  evidence, goal, analysis, claim, dataset, session, and visualization graph.
- Sessions and acquisition outputs, including closing sessions and promoting
  eligible sessions into datasets.
- Dataset staging and direct commit with provenance/manifest capture, without
  an approval gate.
- Analysis, claim, and visualization records as explicit user-driven flows,
  including managed file storage for visualization assets.
- Bounded recent analysis retrieval through `GET /analyses?recent_first=true`,
  so workspace summaries can load the newest committed analyses without scanning
  a project's full analysis history. The analysis and visualization list
  endpoints also accept `since`/`until` time-window bounds, so an assistant can
  pull the advances and plots committed within a window (e.g. since last July)
  as the retrieval backbone for a progress-report draft.
- Goals and goal links as explicit planning and evidence-spanning records
  connected to retained graph entities.
- PROV-O/JSON-LD provenance export, record export events, and external artifact
  references that preserve semantic edges to outside tools without
  reimplementing their workflows. The `lt export` consumer-side command writes
  these documents as self-contained sidecar files that survive without a running
  instance, optionally co-located next to the data files they describe. See
  [provenance-export.md](provenance-export.md).
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
