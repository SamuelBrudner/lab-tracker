# ARA Exploration Graph Design

## Decision

Lab Tracker should represent the divergent research trajectory as first-class
`ExplorationNode` records rather than stretching existing `terminal_reason`,
`QuestionRefactor.reason`, or `ClaimEdge` semantics.

This keeps terminal reasons as end-state annotations and claim edges as
claim-to-claim logic. Exploration nodes become the explicit substrate for
decisions, dead ends, pivots, negative knowledge, and branch convergence.

## Model

Exploration nodes are project-scoped and target one retained graph entity:
question, dataset, analysis, or claim. Targets are polymorphic through the
existing `EntityRef` shape.

Node types:

- `decision`: requires `choice`, `alternatives_considered`, and `rationale`.
- `dead_end`: requires `hypothesis`, `failure_mode`, and `lesson`.
- `pivot`: requires `trigger`, `rationale`, and exactly one invalidated
  exploration node or claim.

Each node carries retained-v1 provenance fields:
`origin`, `change_set_id`, `origin_provider`, `origin_model`, and
`origin_prompt_version`. Staged nodes can be edited; committed nodes are frozen
except archival.

## Edges

Exploration nodes form a DAG with two edge relations:

- `parent`: branch lineage.
- `also_depends_on`: convergence or cross-branch dependency.

The stored direction is prior node -> current node, which makes forward
traversal read like a research history. Service validation rejects duplicate,
self, cross-project, and cyclic edges.

## Human Gate

This slice adds direct API persistence but does not auto-harvest or auto-commit
agent output. Future agent-harvested nodes should enter the existing graph-draft
review queue as proposed changes with `origin=ai_suggested` or
`origin=ai_executed`, and only become committed after human acceptance.

## Export And Graph Surface

Project graph evidence/full views render exploration nodes between claims and
visualizations, with edges to targets, evidence, parent/dependency nodes, and
invalidated claims or nodes.

PROV-O/JSON-LD export emits `lab:ExplorationNode` records with target, evidence,
lineage, dependency, and invalidation links. ARA layered export includes
exploration nodes in logic, trace, and evidence layers, but not the `src` layer
because Lab Tracker intentionally stores code references rather than
reimplementing ARA's code kernel.
