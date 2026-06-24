# ARA Claim Falsification Design

## Decision

Claims should record how they could be wrong, not only how confident the user is
that they are right. Add falsification fields directly to `Claim`; do not create
a separate verification entity for this slice.

## Fields

New claim fields:

- `falsification_criteria`: the condition that would overturn or materially
  weaken the claim.
- `verification_plan`: the experiment or analysis plan that tests the claim.
- `refuting_outcome`: the concrete expected outcome that would refute it.

Existing `ClaimDataset`, `ClaimAnalysis`, and `ClaimQuestion` links remain the
proof pointers. This avoids duplicating the retained provenance spine.

## Status

Add `testing` as an intermediate claim status:

- `proposed` -> `testing`, `supported`, or `rejected`
- `testing` -> `supported` or `rejected`
- `supported` and `rejected` remain terminal

Only proposed claims can change statement, confidence, support links, question
links, citations, or falsification fields. Testing claims can proceed to a final
state without editing the evidentiary content.

## Readiness And Export

Publication readiness flags supported claims that lack falsification criteria.
This extends the existing unsupported-claim gate instead of introducing a new
review system.

The new fields are exposed through HTTP schemas, MCP claim creation, schema
metadata, and PROV-O/JSON-LD claim nodes.

## Deferred

Richer uncertainty models, registered-analysis entities, and separate
verification-plan lifecycles are deferred. The retained-v1 win is explicit
falsifiability on the claim object itself.
