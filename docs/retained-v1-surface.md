# Retained V1 Surface

This document is the single source of truth for the `lab-0rm` cleanup work.
If existing code, UI text, or older docs disagree with this file, this file
defines the supported v1 product surface.

## Decision

The retained v1 product is the smallest workflow set that preserves the core
research record:

- Auth and role-based access control.
- Projects as the top-level container for work.
- Questions created, staged, activated, maintained explicitly by users, and
  connected with `parent_question_ids` to form broad-to-atomic hierarchies.
- Simple query/search flows over questions and notes using the built-in
  substring behavior.
- Manual note capture, including text notes, multipart raw file upload, raw file
  download, raw voice notes with editable transcripts, and attaching notes to
  retained entities.
- On-demand multimodal note-to-graph draft review for uploaded image notes,
  voice-note transcripts, and photo+voice bundles, with human
  edit/accept/reject before commit through normal API validation.
- Sessions and acquisition outputs, including closing sessions and promoting
  eligible sessions into datasets.
- Dataset staging and direct commit with provenance/manifest capture, without
  an approval gate.
- Analysis, claim, and visualization records as explicit user-driven flows,
  including managed file storage for visualization assets.

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
- Automatic question extraction and extraction inbox workflows. The retained
  image-to-graph draft action is explicitly on-demand and note-scoped; it is not
  a standing extraction inbox.
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
