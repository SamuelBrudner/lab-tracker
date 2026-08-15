# Joining an ongoing project

Lab Tracker onboarding does not assume that the science starts when the
software is installed. A researcher can orient Lab Tracker to work already in
progress by establishing one attributed, present-tense checkpoint and then
capturing prospectively from that boundary.

This workflow deliberately does **not** reconstruct a complete project
history. Earlier coverage is labelled selective, and historical material can
be added later through the ordinary note and evidence workflows when it is
useful to a concrete question or decision.

## Completion model

Member onboarding is complete when the authenticated project contributor or
owner has:

1. saved one project-visible checkpoint for the project;
2. resolved each of one to three live-question alignments;
3. received the deterministic current-state map and copyable brief; and
4. stored one separate real text, photo, voice, or bundled capture that targets
   the checkpoint.

The checkpoint itself never counts as the forward capture. A capture queued
offline remains pending until the server stores it.

AI-assisted alignment and shared-record approval are separate milestones. A
member can complete onboarding while accepted AI proposals await a project
owner. Manual mappings written exactly by a contributor use the normal direct
human path and create staged records immediately. An explicit decision to keep
all live questions only in the checkpoint is also a resolved alignment.

## Checkpoint contents and visibility

The checkpoint asks for:

- the current output or decision;
- one to three live research questions;
- the strongest recent result or context;
- the next move; and
- optional pasted aims, project-brief, or meeting context.

The resulting staged note is immutable, targeted to the project, attributed to
the member, and visible to authorized project readers. Its timestamp records
when routine Lab Tracker tracking began for that member. It makes no claim that
the account is a lab consensus or that earlier project history is complete.

Only one checkpoint is supported per project and member in this release.
Corrections and ongoing scientific changes belong in later captures; updated
or superseding checkpoints are deferred.

## Question alignment

Every live question receives an explicit, individual decision. Bulk acceptance
is not available in onboarding.

The manual path can:

- link the checkpoint to an existing active or staged question;
- create the member's exact text as a staged `other` question; or
- retain the question only in the checkpoint.

The AI path is opt-in and requires disclosure and consent before content leaves
the Lab Tracker instance. The provider receives the complete checkpoint plus a
bounded candidate list of up to 30 existing active or staged project questions,
including each question's identifier, text, status, and type. It may propose no
more than three operations, limited to creating a staged question or adding a
link from the checkpoint to one of those existing questions. Goals, claims,
datasets, analyses, activation, parent links, and destructive note updates are
outside the onboarding contract.

The member edits, accepts, or rejects every AI proposal before submission. AI
output retains AI provenance even when a person accepts it; an edited proposal
is recorded as user-revised. Only a project owner can commit accepted AI
operations into the shared graph. A submitted draft with no accepted operations
is resolved without entering the owner queue.

## Current-state brief and map

The brief is deterministic rather than a second model output. It contains the
checkpoint's current output, live questions and their alignment states, strongest
recent context, next move, author and checkpoint time, shared-record timestamp,
and the selective-history warning. Optional pasted source text remains attached
to the checkpoint but is not copied into the brief.

The map distinguishes:

- solid shared active/staged questions;
- outlined member-reviewed AI proposals awaiting an owner;
- muted unreviewed AI proposals; and
- personal checkpoint-only questions.

These styles are curation states, not statements about scientific truth.

## Authorization and failure behavior

Contributors and owners can complete the workflow. Viewers receive the current
read-only orientation and an edit-access request; onboarding does not weaken
generic project permissions. A browser session is required to create the
checkpoint, align questions, review proposals, or commit them. A paired personal
device can read the orientation and make the separate forward capture, but its
advertised onboarding capabilities remain capture-only. Service,
system-automation, and any other non-interactive principals cannot make the
review or commit decisions.

Provider failure never discards the checkpoint or reports a draft as ready.
The manual alignment remains available. Repeated requests reuse the same
checkpoint and generation identity, and provider work is protected by
timeout-aware fenced claims so a stale worker cannot overwrite a newer result.

## HTTP contract

The browser workflow is resumable from one derived resource:

- `GET /projects/{project_id}/member-onboarding` returns the actor's checkpoint,
  alignment state, labelled map, deterministic brief, first stored capture, and
  server-derived capabilities and completion flags.
- `PUT /projects/{project_id}/member-onboarding/checkpoint` creates the one
  checkpoint from `current_output_or_decision`, one to three `live_questions`,
  `strongest_recent_context`, `next_move`, and optional `source_text` and
  `as_of` fields.
- `PUT /projects/{project_id}/member-onboarding/manual-alignment` atomically
  resolves every live question with `link_existing`, `create_staged`, or
  `checkpoint_only`.
- `POST /projects/{project_id}/member-onboarding/ai-alignment` requires
  `external_provider_acknowledged: true`, then creates or resumes the one
  constrained draft.
- `GET /projects/{project_id}/member-onboarding/owner-queue` is an owner-only
  projection of submitted onboarding drafts with accepted operations.

Checkpoint and manual-alignment retries are exact-replay idempotent. Reusing
the same project/member identity with changed content returns a conflict rather
than silently replacing the immutable record. A still-valid generation claim
returns a retryable pending response; successful or failed terminal state is
then visible through the ordinary `GET` resource.

The checkpoint's rendered canonical content is limited to 64,000 characters.
Oversized content is rejected rather than truncated. Provider prompts use the same
bounded checkpoint source, and the current-state brief is generated locally;
copying the brief does not invoke a model.

## Explicitly out of scope

This release does not add chronological history import, completeness scores,
private checkpoints, reorientation, email alerts, automatic external capture
configuration, or onboarding-created goals, claims, datasets, and analyses.
