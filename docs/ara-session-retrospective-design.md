# ARA Session Retrospective Design

## Decision

Session-end retrospective capture should reuse the existing graph-draft batch
queue. Agent conversation summaries may propose exploration nodes, claims, and
notes, but no harvested record commits directly.

This reconciles ARA's low-overhead trajectory harvesting with Lab Tracker's
retained-v1 rule: AI can suggest; only a person commits.

## Capture Shape

A retrospective harvester should summarize the current agent session into a
packet containing:

- decisions made and alternatives considered;
- dead ends, failed hypotheses, and lessons;
- pivots and the claims or prior decisions they invalidate;
- evidence references to retained entities when known;
- provenance tags for whether the agent suggested the idea or executed the
  experiment/analysis.

The harvester writes a `GraphChangeSet` proposal batch, not committed graph
records. Proposed exploration nodes use the vocabulary from
`docs/ara-exploration-graph-design.md`.

## Review Path

The existing daily-review batch surface remains the human gate:

- proposed entities appear in `/app/batches`;
- the user can accept, edit, or reject each operation;
- accepted operations apply through normal API validation;
- accepted AI-suggested edits preserve `origin=ai_suggested`;
- user-edited accepted records preserve `origin=user_revised`;
- agent-run experiments or analyses may use `origin=ai_executed` when the agent
  actually performed the execution.

## Implementation Boundary

This design does not add an always-on background listener or autonomous commit
path. A future implementation should be triggered explicitly at session end or
by a supervised automation, then land proposals in the batch queue.

The harvester should avoid importing ARA's compiler, seal certificate, or code
kernel. Lab Tracker stores semantic reasoning and provenance edges; code and
external artifacts remain referenced by URI and content hash.

## Acceptance For This Epic

The retrospective slice is designed here so the exploration vocabulary and
human-gated landing zone are settled before implementation. Shipping the
harvester itself is a follow-up slice after the exploration-node substrate is in
place.
