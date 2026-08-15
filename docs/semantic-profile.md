# Lab Tracker public semantic profile

This document is the normative class-versus-concept contract for Lab Tracker
provenance JSON-LD, project record exports, and ARA artifacts. It follows the
minimalist Linked Art pattern: keep the structural class model small, put
controlled refinements in classifications, and materialize a relationship only
when the relationship itself has data.

`MUST`, `SHOULD`, and `MAY` below are requirements on the public JSON-LD
surface. They do not change the database, domain models, REST write payloads,
or record lifecycles.

## Admission and typing rules

A Lab Tracker class is admitted only when all three conditions hold:

1. Instances have a stable, independently addressable `@id`.
2. Instances have a distinct property or relationship shape.
3. A consumer needs to branch on that shape.

A lifecycle state, subtype, origin, role, outcome, link state, or relation kind
does not satisfy this rule. It MUST be a URI-addressed controlled concept, not a
class. An edge with no data of its own MUST remain a direct predicate. An edge
MAY become a resource only when role, outcome, status, slot, timestamp, or
provenance metadata must be stated about that edge.

Every materialized resource node MUST carry exactly one explicit,
most-specific `@type`; a pure `{"@id": "…"}` reference need not repeat it.
Superclass facts for custom classes live in the dereferenceable `/terms`
vocabulary and MUST NOT be repeated on instances. In particular:

- a question is `lab:ResearchQuestion`, not both `lab:ResearchQuestion` and
  `prov:Entity`;
- an analysis is `lab:Analysis`, not both `lab:Analysis` and `prov:Activity`;
- an ARA layer is `lab:AraLayer`, not both `lab:AraLayer` and `prov:Bundle`;
- a human is only `prov:Person`, and an AI model agent is only
  `prov:SoftwareAgent`; neither repeats `prov:Agent`.

Generic helper nodes for which this profile admits no custom class use one
standard type such as `prov:Entity` or `prov:Activity`.

## Namespaces and classifications

`lab:` expands to `{canonical-base}/terms#`. Standard terms use:

```text
prov:    http://www.w3.org/ns/prov#
dcterms: http://purl.org/dc/terms/
skos:    http://www.w3.org/2004/02/skos/core#
rdfs:    http://www.w3.org/2000/01/rdf-schema#
```

The JSON key `classifiedAs` MUST expand to `dcterms:type` and MUST have
IRI-valued objects. All controlled concept IRIs have the deterministic form
`lab:<scheme>/<value>`, where `<value>` is the exact serialized enum token
without case conversion or label-derived normalization. For example,
`lab:questionStatus/active` classifies an active research question.

Each `lab:scheme/<scheme>` resource MUST dereference as exactly one
`skos:ConceptScheme`. Each `lab:<scheme>/<value>` resource MUST dereference as
exactly one `skos:Concept`, name `lab:scheme/<scheme>` with `skos:inScheme`, and
have a non-empty human label and definition. Concept definitions live at
`/terms`; they need not be copied into every export.

When a record has more than one classification, `classifiedAs` is an array and
its order has no meaning.

Every custom class below MUST dereference at `/terms` as an `rdfs:Class`, with
a non-empty label and definition and the table's `rdfs:subClassOf` value.

## Class contract

### Core record classes

| Class | Required superclass | Exported record |
| --- | --- | --- |
| `lab:ResearchQuestion` | `prov:Entity` | `Question` |
| `lab:Dataset` | `prov:Entity` | `Dataset` |
| `lab:Claim` | `prov:Entity` | `Claim` |
| `lab:Note` | `prov:Entity` | `Note` |
| `lab:Visualization` | `prov:Entity` | `Visualization` |
| `lab:Goal` | `prov:Entity` | `Goal` |
| `lab:ExplorationNode` | `prov:Entity` | `ExplorationNode` |
| `lab:Analysis` | `prov:Activity` | `Analysis` |
| `lab:AcquisitionSession` | `prov:Activity` | `Session` |

These are the complete core class set. A persistence model or UI resource is
not a public semantic class merely because it exists.

### Qualified relationship classes

| Class | Required superclass | Admission condition |
| --- | --- | --- |
| `lab:QuestionLink` | `prov:Entity` | A dataset-to-question assertion carrying role and outcome |
| `lab:ClaimRelation` | `prov:Entity` | A claim-to-claim assertion carrying relation kind and edge metadata |
| `lab:GoalLink` | `prov:Entity` | A goal-to-record assertion carrying relation kind, link state, or slot |

These classes are first-class only in their qualified form. A future fixed,
unqualified edge with no edge metadata MUST use a direct predicate instead of
one of these resources.

### Provenance extension

| Class | Required superclass | Boundary |
| --- | --- | --- |
| `lab:EntityVersion` | `prov:Entity` | Committed snapshots and materialized pre-revision states |

`EntityVersion` is a provenance extension, not a new kind of core research
record. It identifies a historical state and relates it to the enduring record
IRI with `prov:wasDerivedFrom`.

### ARA extension

| Class | Required superclass | Boundary |
| --- | --- | --- |
| `lab:AraArtifact` | `prov:Entity` | Root of one question- or goal-scoped layered artifact |
| `lab:AraLayer` | `prov:Bundle` | One `logic`, `src`, `trace`, or `evidence` layer |
| `lab:ForensicBinding` | `prov:Entity` | Explicit cross-layer claim/evidence binding |

These three classes form a separately declared ARA extension. They MUST NOT be
treated as core record classes, and base provenance consumers MUST be able to
ignore them without losing the core record graph.

### Standard and generic nodes

| Exported node | Explicit type |
| --- | --- |
| Human account reference | `prov:Person` |
| AI drafting/model agent | `prov:SoftwareAgent` |
| Dataset commit or accepted graph-draft activity | `prov:Activity` |
| Dataset file | `prov:Entity` |
| External artifact declared as an entity | `prov:Entity` |
| External artifact declared as an activity | `prov:Activity` |
| Controlled value definition | `skos:Concept` |
| Controlled scheme definition | `skos:ConceptScheme` |

No custom subclass is admitted for these nodes.

## Controlled concept contract

Every row uses `classifiedAs` / `dcterms:type`. The legacy literal column is
also emitted under the additive compatibility policy below.

| Scheme | Classified resource; legacy literal | Current values |
| --- | --- | --- |
| `lab:questionType` | `ResearchQuestion`; `questionType` | `descriptive`, `hypothesis_driven`, `method_dev`, `other` |
| `lab:questionStatus` | `ResearchQuestion`; `status` | `staged`, `active`, `answered`, `abandoned`, `superseded` |
| `lab:datasetStatus` | `Dataset`; `status` | `staged`, `committed`, `archived` |
| `lab:analysisStatus` | `Analysis`; `status` | `staged`, `committed`, `archived` |
| `lab:claimStatus` | `Claim`; `status` | `proposed`, `testing`, `supported`, `rejected` |
| `lab:noteStatus` | `Note`; `status` | `staged`, `committed`, `archived` |
| `lab:sessionType` | `AcquisitionSession`; `sessionType` | `scientific`, `operational` |
| `lab:sessionStatus` | `AcquisitionSession`; `status` | `active`, `closed` |
| `lab:explorationNodeType` | `ExplorationNode`; `explorationNodeType` | `decision`, `dead_end`, `pivot` |
| `lab:explorationNodeStatus` | `ExplorationNode`; `status` | `staged`, `committed`, `archived` |
| `lab:goalType` | `Goal`; `goalType` | `paper`, `grant`, `talk`, `other` |
| `lab:goalStatus` | `Goal`; `status` | `planned`, `in_progress`, `submitted`, `accepted`, `abandoned` |
| `lab:entityOrigin` | Origin-aware record or pre-revision `EntityVersion`; `origin` | `user`, `ai_suggested`, `ai_executed`, `user_revised` |
| `lab:claimRelation` | `ClaimRelation`; `claimRelationType` | `extends`, `contradicts`, `refutes`, `depends_on`, `supersedes` |
| `lab:questionLinkRole` | `QuestionLink`; `role` | `primary`, `secondary` |
| `lab:outcomeStatus` | `QuestionLink`; `outcomeStatus` | `unknown`, `supports`, `refutes`, `inconclusive` |
| `lab:goalRelation` | `GoalLink`; `role` | `contributes_to`, `addresses`, `candidate_figure`, `supporting_evidence`, `background`, `methods` |
| `lab:goalLinkStatus` | `GoalLink`; `status` | `candidate`, `committed`, `dropped` |

An implementation MUST register a new value in its scheme, with a
dereferenceable definition, before emitting its concept IRI. Reusing an
existing concept IRI for a changed meaning is forbidden.

## Qualified-edge contract

The three qualified patterns are fixed:

| Assertion | Required shape |
| --- | --- |
| Dataset addresses question | Dataset `wasGeneratedBy` commit activity; commit `lab:questionLink` → `QuestionLink`; link `lab:question` → `ResearchQuestion`; link classifications come from `questionLinkRole` and `outcomeStatus` |
| Claim relates to claim | Source `Claim` `lab:claimRelation` → `ClaimRelation`; relation uses `lab:claimRelationSource` and `lab:claimRelationTarget`; relation classification comes from `claimRelation` |
| Goal relates to record | `GoalLink` `lab:goalLink` → owning `Goal` and `lab:target` → target record; classifications come from `goalRelation` and `goalLinkStatus`; optional `slot` remains edge data |

The qualified node owns edge timestamps and any future edge-level provenance.
Exporters MUST NOT mint subclasses such as `SupportingClaimRelation` or
`PrimaryQuestionLink`. They also MUST NOT add a second direct custom predicate
for the same assertion merely as a shortcut.

## Complete export mapping

The following table is exhaustive for node kinds emitted by the current
provenance and ARA builders. Summary nodes and full-detail nodes use the same
type.

| Current record or generated node | Public mapping | Notes |
| --- | --- | --- |
| `Question` | `lab:ResearchQuestion` | Type, status, and origin are concepts |
| `Dataset` | `lab:Dataset` | Status and origin are concepts |
| `Analysis` | `lab:Analysis` | Status and origin are concepts |
| `Claim` | `lab:Claim` | Status and origin are concepts |
| `Note` | `lab:Note` | Status and origin are concepts; server-declared member checkpoints also carry their attributed present-state boundary and selective-history declaration |
| `Visualization` | `lab:Visualization` | Open-ended `vizType` remains a literal |
| `Session` | `lab:AcquisitionSession` | Session type, status, and origin are concepts |
| `Goal` | `lab:Goal` | Currently appears in ARA logic; type, status, and origin are concepts |
| `ExplorationNode` | `lab:ExplorationNode` | Node kind, status, and origin are concepts |
| Dataset question-link entry | `lab:QuestionLink` | Synthetic stable IRI; qualified pattern above |
| `ClaimEdge` | `lab:ClaimRelation` | Stable edge IRI; qualified pattern above |
| `GoalLink` | `lab:GoalLink` | Stable link IRI; qualified pattern above |
| `EntityVersion` | `lab:EntityVersion` | ARA trace provenance extension |
| AI-accepted pre-revision snapshot | `lab:EntityVersion` | Synthetic version IRI; generated by the draft activity |
| ARA artifact wrapper | `lab:AraArtifact` | ARA extension only |
| ARA layer wrapper | `lab:AraLayer` | ARA extension only; one explicit type |
| ARA claim/evidence binding | `lab:ForensicBinding` | ARA extension only |
| `DatasetFile` manifest entry | `prov:Entity` | No custom file class |
| `ExternalArtifactReference` of kind `entity` | `prov:Entity` | Its external URI is its identity |
| `ExternalArtifactReference` of kind `activity` | `prov:Activity` | Its external URI is its identity |
| Generated dataset commit node | `prov:Activity` | Connects files, manifest context, questions, notes, and session |
| Referenced accepted graph-change set | `prov:Activity` | Drafting provenance, not a custom domain class |
| User/creator/executor node | `prov:Person` | One standard most-specific type |
| AI provider/model node | `prov:SoftwareAgent` | One standard most-specific type |

### Relationship mapping

Fixed unqualified relationships remain direct predicates:

| Exported relationship | Public predicate and direction |
| --- | --- |
| Dataset production | `Dataset prov:wasGeneratedBy commit Activity` |
| Analysis inputs | `Analysis prov:used Dataset` or external `prov:Entity` |
| Dataset commit inputs | `commit Activity prov:used DatasetFile` or external `prov:Entity` |
| Prior/external activity context | Activity `prov:wasInformedBy` external or draft `prov:Activity` |
| Visualization production | `Visualization prov:wasGeneratedBy Analysis` |
| Entity authorship | Entity `prov:wasAttributedTo prov:Person` |
| Activity execution | Activity `prov:wasAssociatedWith prov:Person` or `prov:SoftwareAgent` |
| Active supervision | `prov:Person prov:actedOnBehalfOf prov:Person`; current start/end qualifiers remain on the reference object and do not admit a supervision class |
| Question parentage | Child `ResearchQuestion prov:wasDerivedFrom` parent `ResearchQuestion` |
| Exploration parentage | Child `ExplorationNode prov:wasDerivedFrom` parent `ExplorationNode` |
| Accepted note lineage | Derived `Note prov:wasDerivedFrom` antecedent `Note` |
| Visualization data lineage | `Visualization prov:wasDerivedFrom Dataset`; `lab:groundingDataset` preserves the domain-specific assertion |
| Version lineage | `EntityVersion prov:wasDerivedFrom` enduring record |
| Human revision | Revised record `prov:wasRevisionOf EntityVersion` |
| Dataset commit context | Commit `lab:sourceSession` → `AcquisitionSession`; commit `lab:note` → `Note` |
| Session focus | `AcquisitionSession lab:primaryQuestion ResearchQuestion` |
| Claim support | `Claim lab:supportsDataset Dataset`; `Claim lab:supportsAnalysis Analysis` |
| Claim answer | `Claim lab:answersQuestion ResearchQuestion` |
| Claim citation | `Claim lab:cites` external `prov:Entity` or `prov:Activity` |
| Visualization claim association | `Visualization lab:relatedClaim Claim` |
| Note attachment | `Note lab:target` → referenced record |
| Exploration target/evidence | `ExplorationNode lab:target` or `lab:evidence` → referenced record |
| Exploration dependency/invalidation | `ExplorationNode lab:alsoDependsOn ExplorationNode`; `ExplorationNode lab:invalidates` → `ExplorationNode` or `Claim` |
| AI drafting provenance | Origin-aware record or `EntityVersion` `lab:changeSet` → draft activity; entity `prov:wasGeneratedBy` or activity `prov:wasInformedBy` that activity |
| ARA scope | `AraArtifact` or `AraLayer lab:scope` → scoped `ResearchQuestion` or `Goal` |
| ARA layer provenance | `AraLayer prov:wasDerivedFrom AraArtifact` |
| ARA binding index | `AraArtifact lab:crossLayerBinding ForensicBinding` |
| ARA forensic binding | `ForensicBinding` points with `lab:layer`, `lab:claim`, `lab:analysis`, `lab:dataset`, `lab:question`, and `lab:evidence` to the resources it binds |

The ARA `layers` and `crossLayerBindings` members are packaging copies of layer
documents and binding nodes. `codeEnvironment` is an embedded reproducibility
view of already identified analyses. None admits an additional class or
duplicate semantic assertion; the stable `@id` links in the table above are
canonical. Nested reference objects may repeat `entityType` and `entityId` as
routing hints, but those tokens do not type the referenced resource.

## Explicit exclusions

| Operational model or token | Profile decision |
| --- | --- |
| `Project`, project status, groups, memberships, and ownership records | Excluded. A project currently scopes an export request but has no first-class exported node, so there is no `lab:Project` class or project concept scheme. A reference to `/projects/{id}` remains an untyped IRI until Project gains a first-class export in a future profile change. |
| `QuestionRefactor` | Not emitted as a node; resulting question derivation/version facts use the mappings above |
| `AcquisitionOutput` | Not emitted independently; a committed output is represented by its dataset file `prov:Entity` |
| `ProvenanceLink` proposal record | Not emitted as a class. Only a supported accepted relation (currently note-to-note `was_derived_from`) is projected as a direct PROV predicate; proposed and rejected links remain curation state outside the public graph |
| `SupervisionEdge` | Not emitted as a class; projected as `prov:actedOnBehalfOf` |
| Graph-change operations, draft batches, and their lifecycle states | Not public classes or schemes. Only a referenced accepted change set is projected as a standard `prov:Activity` |
| `RecordExport`, export events, usage events, readiness reports, stores, and access-control records | Operational API records, not provenance graph resources |
| Raw note assets, visualization assets, and commit manifests | Described on their owning node or file entities; not additional semantic classes |
| `entityType` reference token | Routing hint, not a class or controlled concept |
| Open-ended `vizType`, `slot`, confidence, free-form metadata, and ARA layer name | Literals, not controlled concepts in this profile |
| `ExternalArtifactKind` | Selects the standard `prov:Entity` or `prov:Activity` type; it does not mint a class or concept |

Project admission requires a later profile decision plus a first-class Project
export. It MUST NOT be inferred from the existence of `/projects/{id}` REST
resources.

## Additive compatibility and evolution

For every controlled value, exporters MUST emit both:

1. the existing literal field, unchanged; and
2. the matching concept IRI through `classifiedAs`.

The two representations MUST be derived from the same serialized enum value
and MUST agree. Concept-aware consumers SHOULD use the IRI; existing consumers
may continue to use the literal. Missing optional values produce neither a
literal nor a classification, except `outcomeStatus=unknown`, which is an
explicit value and therefore produces both.

This rollout is additive: it changes neither record IRIs nor REST write
contracts and requires no persistence migration. Legacy literals may be
removed only by a future, explicitly announced profile-major change. Renaming
a class or scheme, changing a superclass, changing a predicate mapping, or
changing an existing concept's meaning is also profile-major.
