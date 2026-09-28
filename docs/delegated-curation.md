# Delegated curation: letting AI organize the graph on its own

"AI can suggest; only a person commits" stays the default. Delegated curation
is the one exception a project owner can grant on purpose: a bounded class of
AI proposals is applied without waiting for review, and every such change is
recorded as exactly that. This page records what the grant admits, who may act
under it, and how the record stays honest.

## What it is

Two separate switches, set in two places by (possibly) two people:

| Switch | Who sets it | Where | What it decides |
| --- | --- | --- | --- |
| **The grant** (`delegated_curation` on the project-default batch settings) | A project owner, at an interactive session | Review page, "What AI may apply on its own" (`PATCH /projects/{id}/graph-draft-batch-settings/project-default`) | *Which proposals* AI may apply in this project without review |
| **The tool** (a token with the `graph_curate` scope) | Any editor or admin minting for their own agent | Agents page, **Curate graph (delegated)** level (`POST /auth/tokens` with `"scope": "graph_curate"`) | *Which agent* may reach the accept and commit routes |

Neither switch alone does anything. A curate token in a project without a
grant is refused at every accept and commit; a grant without a curate token
still lets the server's own drafting pass act, but no external agent.

## The grant

`delegated_curation` has three values, default `off`:

| Value | Admits | Never admits |
| --- | --- | --- |
| `off` | nothing — every proposal waits for a person | |
| `organize` | `link_note_to_question`, `link_note_to_session`, `link_note_to_dataset`, `link_note_to_analysis`, `link_node_to_goal` | anything that creates a record, closes a question, retires a note, or resolves a claim |
| `full` | every semantic type the drafter can propose | `request_clarification` (it exists to ask a person) |

`organize` is the "keep my captures wired into the graph" setting: links only
add or adjust edges between records that already exist, and a link proposal is
admitted only when its payload carries nothing but the linking field
(`targets` for a note, `links` for a goal) — the same labels map to generic
note and goal updates, so a proposal that also sets a note status or a goal
title is not organizing, whatever it is called. `full` is the
hands-off setting and includes the epistemic proposals — new questions and
notes, `record_decision`/`record_dead_end`/`record_pivot`, `abandon_question`,
`merge_questions`, `retire_note`, and `resolve_prediction` — so choose it
knowing that a claim can be resolved without anyone reading the evidence.

Rules that hold for every grant:

- **Widening needs consent in the same request.** `off -> organize`,
  `off -> full`, and `organize <-> full` each require
  `delegated_curation_acknowledged: true` alongside the new value, from an
  interactive owner session; the row stamps `delegated_curation_granted_at`
  and `delegated_curation_granted_by`. A service token or the scheduler cannot
  grant. Narrowing to `off` needs nothing and clears the stamps.
- **It is project-level only.** Personal settings rows never carry a grant;
  sending `delegated_curation` on the personal endpoint is refused.
- **It applies to every draft the server generates for the project**,
  whoever triggered the drafting: the scheduled review, `POST
  /batches/run-due`, `POST /batches/run-now`, and note-scoped drafts requested
  from a capture or an agent. Member-onboarding proposals are the exception;
  they always need a person.
- **It runs once, on fresh drafts only.** The pass never touches a draft it
  already ran on, or one a person has started deciding (any proposal
  accepted, rejected, or deferred), so re-requesting a draft never re-runs the
  pass over someone's review. A draft a person revises with AI is regenerated
  for that person and is not passed over again.
- **A stopped pass is visible.** If the pass cannot apply a draft (a proposal
  fails validation at apply time, say), the whole pass rolls back as one unit
  in every context — under a request and in the background worker alike —
  the reason is stamped on the change set as
  `error_metadata.delegated_curation_error`, and the draft waits for a person
  exactly as the model left it.
- **No cue for a draft nobody needs to review.** A review-ready email queued
  for a batch draft the pass then commits is dropped at send time.

## The drafting pass

Right after a draft becomes `ready`, the pass runs once under the grant:

1. Accept every still-proposed, valid proposal the grant admits, recording
   `acceptance_mode=auto_accepted`. Proposals outside the grant, invalid
   proposals, and deferred ones stay `proposed`.
2. If anything is still `proposed`, stop: the draft stays in the review queue
   with the admitted proposals pre-accepted, so the person only decides the
   rest (and may re-open a pre-accepted one). Nothing is committed.
3. Otherwise commit, with a message naming the grant. The batch run reports
   `ready` either way; the change set reports `committed` when the pass closed
   it.

The pass records what it did on the change set's
`context_packet.delegated_curation`: the policy, who granted it and when,
which operations it accepted, how many it left, and whether it committed.

## A curate token

A `graph_curate` token is a `stage_evidence` token that can also reach `POST
/batches/run-now`, `PATCH /graph-drafts/{id}/operations/{op}` with
`status=accepted`, `POST /graph-drafts/{id}/accept-all`, and `POST
/graph-drafts/{id}/commit`. The middleware opens those routes; the ordinary
review rules apply first (the token's user must be the draft's author or
assigned reviewer, or a global admin, to touch it at all), then the service
layer decides each call against the project's grant:

- accept-all accepts what the grant admits and leaves the rest proposed;
- a per-operation accept outside the grant is refused (`403`), and so is an
  accept of a proposal a person already rejected or deferred: a person's
  verdict stands;
- a commit is refused while any proposal is undecided, or while any accepted
  proposal is outside the grant, and still requires the token's user to be a
  project owner (the ordinary commit rule);
- editing a payload, adding a review note, rejecting, deferring, submitting,
  and reviewing are refused: a delegated principal may only accept.

Its direct writes keep the `stage_evidence` body rules: notes are created
`staged` only and evidence bundles preview only. An `all`-scope token is never
admitted to the delegated gate; the tool permission is minted on purpose.

The MCP tools are `lab_tracker_run_graph_draft_batch`,
`lab_tracker_get_graph_draft`, `lab_tracker_accept_graph_draft_operations`,
and `lab_tracker_commit_graph_draft`. The server instructions and the managed
`CLAUDE.md`/`AGENTS.md` block tell every agent the same thing: use them only
when the user asks, expect a refusal outside the grant, and treat draft text
as untrusted data.

## What the record says

- Each delegated accept carries `acceptance_mode=auto_accepted`, `accepted_at`,
  and `accepted_by` = the **person of record**: the token's owner for a curate
  token, the granting owner for the drafting pass. The label is what says
  nobody looked; the person is whose authority it acted under.
- A delegated commit stamps `committed_by` the same way and a commit message
  that names the grant, and the records it applies are attributed
  (`created_by`) to that person with `origin=ai_suggested` and the change-set
  backlink — as a person's own commit of an AI proposal would attribute them.
- PROV-O export classifies the produced record with
  `lab:acceptanceMode/auto_accepted` and `acceptedBy` the person of record.
- The draft-quality ledger counts `accepted_auto_accepted` beside
  `accepted_human_selected` and `accepted_bulk_accepted`, so a project can see
  how much of its graph nobody reviewed.
- The review page badges each auto-accepted proposal and the provenance
  panel says what the pass applied or why it stopped.

## What it does not change

- The default. A project with no grant behaves exactly as before; the
  structural gate (`require_interactive`) still refuses every non-interactive
  principal.
- Human review verdicts. Only a person edits, rejects, defers, submits, or
  reviews; only a person grants or widens delegation.
- Direct writes. The grant covers AI-drafted proposals only; it does not turn
  a curate token into an `all`-scope token.
- Onboarding. Member-onboarding proposals are never delegated.
