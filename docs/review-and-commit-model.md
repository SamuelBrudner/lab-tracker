# Review and commit model: why the gates are asymmetric

Lab Tracker gates AI-proposed changes behind human review, but lets people
commit directly. That asymmetry is deliberate, and this note records it so it is
read as a design decision rather than an oversight.

## The decision

**People commit directly; only AI proposals are human-gated.** There is no
mandatory peer- or PI-approval step on human-authored records, and there is no
default separation-of-duties requirement. The human review energy is spent
entirely on machine output.

This follows the retained-v1 surface, which deliberately defers dataset review
policy, request, queue, and UI, and keeps direct commit "without an approval
gate." Second-person approval is a possible *optional governance* layer on top
of direct commit, not the default lifecycle. See
[retained-v1-surface.md](retained-v1-surface.md) (Deferred Workflows and the
Restoration Ledger).

## Who can do what

**Direct human writes.** Creating and committing a dataset, analysis, claim, or
visualization needs only project `contributor` rights. Nothing else reviews it.
The record's honesty comes from the enforced graph invariants — the Birth
Requirement, content-addressed commit hashes, evidence-backed `supported` claims
— not from a second person signing off.

**AI graph-draft review.** An AI draft is proposed, never committed. The gate is
the person who turns proposals into graph edges:

- The draft **author** (a contributor) accepts, edits, or rejects each proposed
  operation, and may submit the draft for review.
- Only a project **owner** commits, and commit requires at least one accepted
  operation and a non-empty message.
- The `review` action can only *reject* or *request changes* — there is no
  separate "approved" state. An owner expresses approval by committing.
- Commit is allowed from `ready` as well as `submitted`, so the
  submit -> review -> commit loop is available but not required.

A consequence worth stating plainly: a *contributor*-authored draft does require
an owner to commit it, so there is real separation there; an *owner*-authored
draft is committed by that same owner. That is intended — the solo scientist
reviewing and committing their own AI drafts is the primary flow, not an edge
case to be gated away.

Only interactive human sessions operate this gate at all: a delegated service
token or an automation principal may draft but can never accept or commit. See
the human-commit gate in `auth.py` / `project_authorization.py`.

## What is deliberately not enforced

- **A mandatory review step on human writes.** Deferred; would resurrect the
  retired dataset-review workflow.
- **Author != committer separation of duties.** Not required by default; it
  would break the solo-scientist flow. If a lab wants it, it belongs behind a
  per-project opt-in (optional governance), added on top of direct commit rather
  than replacing it.

## How honesty is preserved without a peer gate

The graph stays "honest about its own curation" through provenance, not through
forced review:

- Each accepted AI operation records **how** it was accepted (`human_selected`
  vs `bulk_accepted`), so a rubber-stamped batch is never mistaken for
  per-operation scrutiny. See [curation-states.md](curation-states.md).
- Every applied entity carries an `origin` (`user` / `ai_suggested` /
  `user_revised`) plus the change set, provider, model, and prompt version, so
  AI contribution and human curation remain distinguishable in PROV-O export.
- Set-aside captures name a reason, so skipped review degrades visible coverage
  rather than silent trust.

The boundary, in one line: **enforce structural invariants and record
provenance on every path; require a second person only for AI output, and only
optionally for humans.**
