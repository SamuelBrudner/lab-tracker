# Retained V1 Surface

This document is the single source of truth for the `lab-0rm` cleanup work.
If existing code, UI text, or older docs disagree with this file, this file
defines the supported v1 product surface.

## Decision

The retained v1 product is the smallest workflow set that preserves the core
research record:

- Auth and role-based access control.
- Projects as the top-level container for work.
- Questions created, staged, activated, and maintained explicitly by users.
- Simple query/search flows over questions and notes using the built-in
  substring behavior.
- Manual note capture, including text notes, multipart raw file upload, raw file
  download, and attaching notes to retained entities.
- On-demand image-to-graph draft review for a single uploaded image note, with
  human edit/accept/reject before commit through normal API validation.
- Sessions and acquisition outputs, including closing sessions and promoting
  eligible sessions into datasets.
- Dataset staging and direct commit with provenance/manifest capture, without
  an approval gate.
- Analysis, claim, and visualization records as explicit user-driven flows.

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
