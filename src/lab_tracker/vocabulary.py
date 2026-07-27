"""Single source of truth for the public provenance vocabulary.

Every property, custom class, and controlled concept emitted in a
PROV-O/JSON-LD provenance document is declared here with its IRI, semantic
kind, and a human-readable definition. The registry generates both the
``@context`` embedded in every document (see :mod:`lab_tracker.provenance`)
and the ``GET /terms`` vocabulary page that ``lab:`` IRIs dereference to —
so the context and its documentation cannot drift apart.

Naming policy: a key maps to a standard vocabulary IRI (PROV-O, schema.org,
Dublin Core) whenever one exists; the ``lab:`` namespace is reserved for
genuinely domain-specific research-record concepts. Deprecated keys stay in
the context as aliases of their replacement's IRI for one release so older
documents remain interpretable, but builders no longer emit them.
Compatibility aliases may remain emitted while a legacy document shape is
still supported.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

PREFIXES: dict[str, str] = {
    "prov": "http://www.w3.org/ns/prov#",
    "schema": "https://schema.org/",
    "dcterms": "http://purl.org/dc/terms/",
}

_VOCABULARY_PREFIXES: dict[str, str] = {
    **PREFIXES,
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
}


class TermKind(str, Enum):
    """Semantic role a term plays in the public vocabulary."""

    CLASS = "class"
    PROPERTY = "property"
    CONCEPT = "concept"
    CONCEPT_SCHEME = "concept_scheme"


@dataclass(frozen=True)
class Term:
    """One typed vocabulary term and its optional semantic metadata."""

    name: str
    iri: str
    definition: str
    kind: TermKind = TermKind.PROPERTY
    is_id: bool = False
    is_json: bool = False
    deprecated_alias_of: str | None = None
    compatibility_alias_of: str | None = None
    superclass: str | None = None
    domain: str | None = None
    range: str | None = None
    emitters: tuple[str, ...] = ()
    in_scheme: str | None = None


@dataclass(frozen=True)
class ConceptScheme:
    """A controlled concept scheme and the values currently admitted to it."""

    name: str
    definition: str
    values: tuple[str, ...]


CONCEPT_SCHEMES: tuple[ConceptScheme, ...] = (
    ConceptScheme(
        "questionType",
        "Kinds of research question represented by Lab Tracker.",
        ("descriptive", "hypothesis_driven", "method_dev", "other"),
    ),
    ConceptScheme(
        "questionStatus",
        "Lifecycle states of a research question.",
        ("staged", "active", "answered", "abandoned", "superseded"),
    ),
    ConceptScheme(
        "datasetStatus",
        "Lifecycle states of a dataset record.",
        ("staged", "committed", "archived"),
    ),
    ConceptScheme(
        "analysisStatus",
        "Lifecycle states of an analysis record.",
        ("staged", "committed", "archived"),
    ),
    ConceptScheme(
        "claimStatus",
        "Lifecycle and evidential states of a research claim.",
        ("proposed", "testing", "supported", "rejected"),
    ),
    ConceptScheme(
        "noteStatus",
        "Lifecycle states of a research note.",
        ("staged", "committed", "archived"),
    ),
    ConceptScheme(
        "sessionType",
        "Kinds of acquisition or experiment session.",
        ("scientific", "operational"),
    ),
    ConceptScheme(
        "sessionStatus",
        "Lifecycle states of an acquisition or experiment session.",
        ("active", "closed"),
    ),
    ConceptScheme(
        "explorationNodeType",
        "Kinds of divergent research-trajectory event.",
        ("decision", "dead_end", "pivot"),
    ),
    ConceptScheme(
        "explorationNodeStatus",
        "Lifecycle states of a research exploration node.",
        ("staged", "committed", "archived"),
    ),
    ConceptScheme(
        "goalType",
        "Kinds of research output goal.",
        ("paper", "grant", "talk", "other"),
    ),
    ConceptScheme(
        "goalStatus",
        "Lifecycle states of a research output goal.",
        ("planned", "in_progress", "submitted", "accepted", "abandoned"),
    ),
    ConceptScheme(
        "entityOrigin",
        "Ways a record entered the human-gated research graph.",
        ("user", "ai_suggested", "ai_executed", "user_revised"),
    ),
    ConceptScheme(
        "claimRelation",
        "Qualified logical relations between research claims.",
        ("extends", "contradicts", "refutes", "depends_on", "supersedes"),
    ),
    ConceptScheme(
        "questionLinkRole",
        "Roles a question plays for a committed dataset.",
        ("primary", "secondary"),
    ),
    ConceptScheme(
        "outcomeStatus",
        "Outcomes a dataset records for a linked question.",
        ("unknown", "supports", "refutes", "inconclusive"),
    ),
    ConceptScheme(
        "goalRelation",
        "Ways a graph record contributes to a research output goal.",
        (
            "contributes_to",
            "addresses",
            "candidate_figure",
            "supporting_evidence",
            "background",
            "methods",
        ),
    ),
    ConceptScheme(
        "goalLinkStatus",
        "Curation states of a qualified goal link.",
        ("candidate", "committed", "dropped"),
    ),
)

_CONCEPT_VALUES_BY_SCHEME: dict[str, frozenset[str]] = {
    scheme.name: frozenset(scheme.values) for scheme in CONCEPT_SCHEMES
}


def concept_iri(scheme: str, value: str) -> str:
    """Return the compact IRI for a registered controlled-concept value."""

    values = _CONCEPT_VALUES_BY_SCHEME.get(scheme)
    if values is None:
        raise ValueError(f"Unknown concept scheme: {scheme}")
    if value not in values:
        raise ValueError(f"Unknown value for concept scheme {scheme}: {value}")
    return f"lab:{scheme}/{value}"


_PROPERTY_TERMS: tuple[Term, ...] = (
    # --- PROV-O relations (bare keys aliased to the standard IRIs) ---
    Term(
        "wasGeneratedBy",
        "prov:wasGeneratedBy",
        "The activity that produced this entity.",
        is_id=True,
    ),
    Term(
        "wasAttributedTo",
        "prov:wasAttributedTo",
        "The agent (person or software) responsible for this entity.",
        is_id=True,
    ),
    Term(
        "wasDerivedFrom",
        "prov:wasDerivedFrom",
        "The entity this entity was derived from.",
        is_id=True,
    ),
    Term(
        "wasRevisionOf",
        "prov:wasRevisionOf",
        "The earlier version this entity revises.",
        is_id=True,
    ),
    Term(
        "used",
        "prov:used",
        "An entity this activity consumed as input.",
        is_id=True,
    ),
    Term(
        "wasInformedBy",
        "prov:wasInformedBy",
        "An activity whose outputs informed this activity.",
        is_id=True,
    ),
    Term(
        "wasAssociatedWith",
        "prov:wasAssociatedWith",
        "An agent that carried out this activity.",
        is_id=True,
    ),
    Term(
        "actedOnBehalfOf",
        "prov:actedOnBehalfOf",
        "The agent this agent was working under — in a lab, the supervision "
        "relationship active when the work happened.",
        is_id=True,
    ),
    # --- Timestamps (standard IRIs; keys unchanged) ---
    Term(
        "createdAt",
        "dcterms:created",
        "When the record was created, ISO 8601.",
    ),
    Term(
        "updatedAt",
        "dcterms:modified",
        "When the record was last modified, ISO 8601.",
    ),
    Term(
        "generatedAt",
        "prov:generatedAtTime",
        "When the entity came into existence, ISO 8601.",
    ),
    Term(
        "startedAt",
        "prov:startedAtTime",
        "When the activity began, ISO 8601.",
    ),
    Term(
        "endedAt",
        "prov:endedAtTime",
        "When the activity ended, ISO 8601.",
    ),
    Term(
        "executedAt",
        "lab:executedAt",
        "When the analysis run was executed, ISO 8601.",
    ),
    # --- File and asset description (schema.org where it exists) ---
    Term(
        "contentUrl",
        "schema:contentUrl",
        "URL where the described file's bytes can be fetched.",
        is_id=True,
    ),
    Term(
        "contentSize",
        "schema:contentSize",
        "Size of the described file in bytes.",
    ),
    Term(
        "encodingFormat",
        "schema:encodingFormat",
        "MIME type of the described file.",
    ),
    Term(
        "caption",
        "schema:caption",
        "Human-written caption for a visualization.",
    ),
    Term(
        "fileName",
        "lab:fileName",
        "Base name of the described file.",
    ),
    Term(
        "filePath",
        "lab:filePath",
        "Logical path of the file relative to the lab's registered data "
        "root; the server stores no absolute machine paths.",
    ),
    Term(
        "checksum",
        "lab:checksum",
        "Content hash (SHA-256, hex) of the described file's bytes.",
    ),
    Term(
        "sha256",
        "lab:checksum",
        "Deprecated alias of checksum; no longer emitted.",
        deprecated_alias_of="checksum",
    ),
    Term(
        "sizeBytes",
        "schema:contentSize",
        "Deprecated alias of contentSize; no longer emitted.",
        deprecated_alias_of="contentSize",
    ),
    # --- Identity and typing of records ---
    Term(
        "entityType",
        "lab:entityType",
        "Lab Tracker record kind this node describes or points at "
        "(question, dataset, analysis, claim, …).",
    ),
    Term(
        "entityId",
        "lab:entityId",
        "UUID of the Lab Tracker record this node describes or points at.",
    ),
    Term(
        "status",
        "lab:status",
        "Lifecycle status of the record (for example staged, active, "
        "committed, supported, archived).",
    ),
    Term(
        "terminalReason",
        "lab:terminalReason",
        "Stated reason a record reached a terminal status.",
    ),
    Term(
        "versionNumber",
        "lab:versionNumber",
        "Monotonic version number of this committed entity version.",
    ),
    Term(
        "userId",
        "lab:userId",
        "UUID of the user account behind a prov:Person node.",
    ),
    Term(
        "classifiedAs",
        "dcterms:type",
        "A stable controlled concept that classifies this resource while "
        "legacy literal fields remain available.",
        is_id=True,
        domain="rdfs:Resource",
        range="skos:Concept",
        emitters=("_classify",),
    ),
    # --- Questions: the spine of the record ---
    Term(
        "text",
        "lab:text",
        "The question text as the researcher wrote it.",
    ),
    Term(
        "questionType",
        "lab:questionType",
        "Kind of question (for example descriptive, causal, mechanistic).",
    ),
    Term(
        "hypothesis",
        "lab:hypothesis",
        "The expectation stated before data was collected.",
    ),
    Term(
        "question",
        "lab:question",
        "The question a record points back at.",
        is_id=True,
    ),
    Term(
        "primaryQuestion",
        "lab:primaryQuestion",
        "The question a dataset was collected to address; required at "
        "commit.",
        is_id=True,
    ),
    Term(
        "questionLink",
        "lab:questionLink",
        "A dataset-to-question link recorded in the commit manifest, with "
        "its role and outcome.",
        is_id=True,
    ),
    Term(
        "answersQuestion",
        "lab:answersQuestion",
        "The question this claim answers.",
        is_id=True,
    ),
    Term(
        "role",
        "lab:role",
        "Role of a link (for example primary or secondary for question "
        "links; the relation kind for goal links).",
    ),
    Term(
        "outcomeStatus",
        "lab:outcomeStatus",
        "What the data said about the linked question: supported, refuted, "
        "or inconclusive.",
    ),
    # --- Datasets and acquisition ---
    Term(
        "commitHash",
        "lab:commitHash",
        "Hash over the dataset's committed manifest, fixing its contents.",
    ),
    Term(
        "sourceSession",
        "lab:sourceSession",
        "The acquisition session the dataset's files came from.",
        is_id=True,
    ),
    Term(
        "sessionType",
        "lab:sessionType",
        "Kind of acquisition session (for example operational, pilot).",
    ),
    Term(
        "metadata",
        "lab:metadata",
        "Free-form key-value metadata recorded in the commit manifest.",
        is_json=True,
    ),
    Term(
        "nwbMetadata",
        "lab:nwbMetadata",
        "NWB (Neurodata Without Borders) metadata block recorded at "
        "commit.",
        is_json=True,
    ),
    Term(
        "bidsMetadata",
        "lab:bidsMetadata",
        "BIDS (Brain Imaging Data Structure) metadata block recorded at "
        "commit.",
        is_json=True,
    ),
    # --- Analyses and reproducibility ---
    Term(
        "analysis",
        "lab:analysis",
        "The analysis a record belongs to or was produced by.",
        is_id=True,
    ),
    Term(
        "codeVersion",
        "lab:codeVersion",
        "Version identifier of the analysis code (typically a git commit).",
    ),
    Term(
        "methodHash",
        "lab:methodHash",
        "Hash identifying the analysis method or pipeline definition.",
    ),
    Term(
        "environmentHash",
        "lab:environmentHash",
        "Hash identifying the software environment the analysis ran in.",
    ),
    # --- Claims and falsification ---
    Term(
        "statement",
        "lab:statement",
        "The claim's assertion as the researcher wrote it.",
    ),
    Term(
        "confidence",
        "lab:confidence",
        "The researcher's stated confidence in the claim.",
    ),
    Term(
        "falsificationCriteria",
        "lab:falsificationCriteria",
        "What observation would falsify this claim, stated up front.",
    ),
    Term(
        "refutingOutcome",
        "lab:refutingOutcome",
        "The outcome that would count as refuting the claim.",
    ),
    Term(
        "verificationPlan",
        "lab:verificationPlan",
        "How the claim is meant to be verified.",
    ),
    Term(
        "supportsDataset",
        "lab:supportsDataset",
        "A committed dataset offered as support for this claim.",
        is_id=True,
    ),
    Term(
        "supportsAnalysis",
        "lab:supportsAnalysis",
        "A committed analysis offered as support for this claim.",
        is_id=True,
    ),
    Term(
        "claimRelation",
        "lab:claimRelation",
        "A typed edge between two claims.",
        is_id=True,
    ),
    Term(
        "claimRelationSource",
        "lab:claimRelationSource",
        "Source claim of a claim relation.",
        is_id=True,
    ),
    Term(
        "claimRelationTarget",
        "lab:claimRelationTarget",
        "Target claim of a claim relation.",
        is_id=True,
    ),
    Term(
        "claimRelationType",
        "lab:claimRelationType",
        "Kind of claim relation (for example supports, contradicts, "
        "refines).",
    ),
    Term(
        "relatedClaim",
        "lab:relatedClaim",
        "A claim this visualization relates to.",
        is_id=True,
    ),
    Term(
        "cites",
        "lab:cites",
        "An external artifact (for example a publication) the claim cites.",
        is_id=True,
    ),
    # --- Notes and captures ---
    Term(
        "note",
        "lab:note",
        "A research note attached to the record.",
        is_id=True,
    ),
    Term(
        "rawContent",
        "lab:rawContent",
        "The note's content as captured, before any transcription.",
    ),
    Term(
        "transcribedText",
        "lab:transcribedText",
        "Machine transcription of a captured voice note.",
    ),
    # --- Visualizations ---
    Term(
        "vizType",
        "lab:vizType",
        "Kind of visualization (for example figure, table).",
    ),
    # --- Exploration nodes: decisions, dead ends, pivots ---
    Term(
        "explorationNode",
        "lab:explorationNode",
        "A recorded decision, dead end, or pivot in the research "
        "trajectory.",
        is_id=True,
    ),
    Term(
        "explorationNodeType",
        "lab:explorationNodeType",
        "Kind of exploration node: decision, dead end, or pivot.",
    ),
    Term(
        "choice",
        "lab:choice",
        "The option the researcher chose at a decision point.",
    ),
    Term(
        "alternativesConsidered",
        "lab:alternativesConsidered",
        "The options considered and not taken at a decision point.",
        is_json=True,
    ),
    Term(
        "rationale",
        "lab:rationale",
        "Why the choice was made, or why a change was proposed.",
    ),
    Term(
        "lesson",
        "lab:lesson",
        "What a dead end taught; why the path was abandoned.",
    ),
    Term(
        "failureMode",
        "lab:failureMode",
        "How the abandoned approach failed.",
    ),
    Term(
        "trigger",
        "lab:trigger",
        "What prompted a pivot or exploration step.",
    ),
    Term(
        "toolingContext",
        "lab:toolingContext",
        "The tools or setting the exploration step happened in.",
    ),
    Term(
        "invalidates",
        "lab:invalidates",
        "A record this exploration node invalidates.",
        is_id=True,
    ),
    Term(
        "alsoDependsOn",
        "lab:alsoDependsOn",
        "Additional records this exploration node depends on.",
        is_id=True,
    ),
    Term(
        "target",
        "lab:target",
        "The record an exploration node or goal link points at.",
        is_id=True,
    ),
    Term(
        "evidence",
        "lab:evidence",
        "A record offered as evidence for this node.",
        is_id=True,
    ),
    Term(
        "groundingDataset",
        "lab:groundingDataset",
        "A committed dataset grounding this node in real data.",
        is_id=True,
    ),
    # --- Goals ---
    Term(
        "goalType",
        "lab:goalType",
        "Kind of research output goal (for example paper, grant, or talk).",
        domain="lab:Goal",
        range="rdfs:Literal",
        emitters=("_goal_node",),
    ),
    Term(
        "goalLink",
        "lab:goalLink",
        "A typed link from a goal to another record.",
        is_id=True,
        domain="lab:GoalLink",
        range="lab:Goal",
        emitters=("_goal_link_node",),
    ),
    Term(
        "summary",
        "lab:summary",
        "Human-readable summary of a research output goal.",
        domain="lab:Goal",
        range="rdfs:Literal",
        emitters=("_goal_node",),
    ),
    Term(
        "slot",
        "lab:slot",
        "Named position a qualified goal link occupies in its output.",
        domain="lab:GoalLink",
        range="rdfs:Literal",
        emitters=("_goal_link_node",),
    ),
    # --- AI drafting attribution: agents propose, people commit ---
    Term(
        "origin",
        "lab:origin",
        "How the record entered the graph: written by a person, or "
        "AI-suggested and human-accepted.",
    ),
    Term(
        "changeSet",
        "lab:changeSet",
        "The AI-drafted change set this record was accepted from.",
        is_id=True,
    ),
    Term(
        "aiProvider",
        "lab:aiProvider",
        "AI provider whose model drafted the proposal.",
    ),
    Term(
        "aiModel",
        "lab:aiModel",
        "Model that drafted the proposal.",
    ),
    Term(
        "aiPromptVersion",
        "lab:aiPromptVersion",
        "Version of the drafting prompt in use when the proposal was made.",
    ),
    # --- Supervision ---
    Term(
        "supervisionStartedAt",
        "lab:supervisionStartedAt",
        "When the supervision relationship began, ISO 8601.",
    ),
    Term(
        "supervisionEndedAt",
        "lab:supervisionEndedAt",
        "When the supervision relationship ended, ISO 8601.",
    ),
    # --- External artifacts: records that live outside Lab Tracker ---
    Term(
        "externalArtifact",
        "lab:externalArtifact",
        "A reference to an artifact that lives outside Lab Tracker.",
        is_id=True,
    ),
    Term(
        "externalUri",
        "lab:externalUri",
        "URI of the external artifact in its home system.",
        is_id=True,
    ),
    Term(
        "externalSourceSystem",
        "lab:externalSourceSystem",
        "System the external artifact lives in (for example a git host or "
        "an archive).",
    ),
    Term(
        "externalContentHash",
        "lab:externalContentHash",
        "Content hash of the external artifact at reference time.",
    ),
    Term(
        "externalMetadata",
        "lab:externalMetadata",
        "Free-form metadata captured about the external artifact.",
        is_json=True,
    ),
    # --- ARA artifacts: layered question/goal exports ---
    Term(
        "scope",
        "lab:scope",
        "The goal or question subtree an ARA artifact covers.",
        is_id=True,
    ),
    Term(
        "layer",
        "lab:layer",
        "Which ARA layer this document is (logic, src, trace, or "
        "evidence).",
    ),
    Term(
        "layers",
        "lab:layers",
        "The full set of layer documents in an ARA artifact.",
        is_json=True,
    ),
    Term(
        "crossLayerBinding",
        "lab:crossLayerBinding",
        "A node that appears in more than one ARA layer, binding them.",
        is_id=True,
        domain="prov:Entity",
        range="lab:ForensicBinding",
        emitters=("build_ara_artifact_document",),
    ),
    Term(
        "crossLayerBindings",
        "lab:crossLayerBinding",
        "Compatibility key for embedded cross-layer binding nodes; equivalent "
        "to crossLayerBinding.",
        compatibility_alias_of="crossLayerBinding",
        domain="prov:Entity",
        range="lab:ForensicBinding",
        emitters=("build_ara_artifact_document", "_build_ara_layer_document"),
    ),
    Term(
        "claim",
        "lab:claim",
        "The claim anchored by an ARA forensic binding.",
        is_id=True,
        domain="lab:ForensicBinding",
        range="lab:Claim",
        emitters=("_claim_cross_layer_bindings",),
    ),
    Term(
        "dataset",
        "lab:dataset",
        "A dataset connected to an ARA forensic binding.",
        is_id=True,
        domain="lab:ForensicBinding",
        range="lab:Dataset",
        emitters=("_claim_cross_layer_bindings",),
    ),
    Term(
        "codeEnvironment",
        "lab:codeEnvironment",
        "Analysis code and environment identifiers collected by an ARA "
        "forensic binding.",
        domain="lab:ForensicBinding",
        range="lab:Analysis",
        emitters=("_claim_cross_layer_bindings",),
    ),
)

_CLASS_TERMS: tuple[Term, ...] = (
    # --- Core public application profile ---
    Term(
        "ResearchQuestion",
        "lab:ResearchQuestion",
        "A durable research question that organizes evidence and interpretation.",
        kind=TermKind.CLASS,
        superclass="prov:Entity",
        emitters=("_question_node",),
    ),
    Term(
        "Dataset",
        "lab:Dataset",
        "A versioned collection of research data committed against a question.",
        kind=TermKind.CLASS,
        superclass="prov:Entity",
        emitters=("build_dataset_provenance_document", "_dataset_summary_node"),
    ),
    Term(
        "Claim",
        "lab:Claim",
        "A falsifiable research assertion linked to questions and evidence.",
        kind=TermKind.CLASS,
        superclass="prov:Entity",
        emitters=("_claim_node",),
    ),
    Term(
        "Note",
        "lab:Note",
        "A human research record captured in its original form.",
        kind=TermKind.CLASS,
        superclass="prov:Entity",
        emitters=("_note_node",),
    ),
    Term(
        "Visualization",
        "lab:Visualization",
        "A figure, plot, table, or other visual research artifact.",
        kind=TermKind.CLASS,
        superclass="prov:Entity",
        emitters=("_visualization_node",),
    ),
    Term(
        "Goal",
        "lab:Goal",
        "A planned research output such as a paper, grant, or talk.",
        kind=TermKind.CLASS,
        superclass="prov:Entity",
        emitters=("_goal_node",),
    ),
    Term(
        "ExplorationNode",
        "lab:ExplorationNode",
        "A decision, dead end, or pivot in the divergent research trajectory.",
        kind=TermKind.CLASS,
        superclass="prov:Entity",
        emitters=("_exploration_node_node",),
    ),
    Term(
        "Analysis",
        "lab:Analysis",
        "A reproducible research analysis that consumes datasets.",
        kind=TermKind.CLASS,
        superclass="prov:Activity",
        emitters=("build_analysis_provenance_document", "_analysis_summary_node"),
    ),
    Term(
        "AcquisitionSession",
        "lab:AcquisitionSession",
        "An acquisition or experiment session that produces research data.",
        kind=TermKind.CLASS,
        superclass="prov:Activity",
        emitters=("_session_node",),
    ),
    # --- Qualified relationships ---
    Term(
        "ClaimRelation",
        "lab:ClaimRelation",
        "A qualified logical relationship between two research claims.",
        kind=TermKind.CLASS,
        superclass="prov:Entity",
        emitters=("_claim_relation_node",),
    ),
    Term(
        "QuestionLink",
        "lab:QuestionLink",
        "A qualified dataset-to-question relationship carrying role and outcome.",
        kind=TermKind.CLASS,
        superclass="prov:Entity",
        emitters=("_dataset_question_link_node",),
    ),
    Term(
        "GoalLink",
        "lab:GoalLink",
        "A qualified goal-to-record relationship carrying relation, state, and slot.",
        kind=TermKind.CLASS,
        superclass="prov:Entity",
        emitters=("_goal_link_node",),
    ),
    # --- Provenance and ARA extensions ---
    Term(
        "EntityVersion",
        "lab:EntityVersion",
        "An immutable historical version of a graph entity.",
        kind=TermKind.CLASS,
        superclass="prov:Entity",
        emitters=("_entity_version_node", "_origin_provenance_nodes"),
    ),
    Term(
        "AraArtifact",
        "lab:AraArtifact",
        "A layered, scope-bounded research artifact compiled for an output or question.",
        kind=TermKind.CLASS,
        superclass="prov:Entity",
        emitters=("build_ara_artifact_document",),
    ),
    Term(
        "AraLayer",
        "lab:AraLayer",
        "One independently retrievable logic, source, trace, or evidence layer of "
        "an ARA artifact.",
        kind=TermKind.CLASS,
        superclass="prov:Bundle",
        emitters=("_build_ara_layer_document",),
    ),
    Term(
        "ForensicBinding",
        "lab:ForensicBinding",
        "An ARA cross-layer binding that connects a claim to its research trace.",
        kind=TermKind.CLASS,
        superclass="prov:Entity",
        emitters=("_claim_cross_layer_bindings",),
    ),
)


def _concept_terms() -> tuple[Term, ...]:
    terms: list[Term] = []
    for scheme in CONCEPT_SCHEMES:
        scheme_iri = f"lab:scheme/{scheme.name}"
        terms.append(
            Term(
                f"scheme/{scheme.name}",
                scheme_iri,
                scheme.definition,
                kind=TermKind.CONCEPT_SCHEME,
            )
        )
        for value in scheme.values:
            readable_value = value.replace("_", " ")
            terms.append(
                Term(
                    f"{scheme.name}/{value}",
                    concept_iri(scheme.name, value),
                    f"{readable_value.capitalize()} in the {scheme.name} concept scheme.",
                    kind=TermKind.CONCEPT,
                    in_scheme=scheme_iri,
                )
            )
    return tuple(terms)


TERMS: tuple[Term, ...] = (*_CLASS_TERMS, *_PROPERTY_TERMS, *_concept_terms())

_TERMS_BY_NAME: dict[str, Term] = {term.name: term for term in TERMS}

_RDF_TYPE_BY_TERM_KIND: dict[TermKind, str] = {
    TermKind.CLASS: "rdfs:Class",
    TermKind.PROPERTY: "rdf:Property",
    TermKind.CONCEPT: "skos:Concept",
    TermKind.CONCEPT_SCHEME: "skos:ConceptScheme",
}


def term_iri(name: str) -> str:
    """Return a registered compact IRI for semantic projection mappings."""

    try:
        return _TERMS_BY_NAME[name].iri
    except KeyError as exc:
        raise ValueError(f"Unknown vocabulary term: {name}") from exc


def terms_namespace(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/terms#"


def build_context(base_url: str) -> dict[str, object]:
    """The ``@context`` embedded in every provenance document."""
    context: dict[str, object] = dict(PREFIXES)
    context["lab"] = terms_namespace(base_url)
    for term in TERMS:
        if term.kind != TermKind.PROPERTY:
            continue
        if term.is_id:
            context[term.name] = {"@id": term.iri, "@type": "@id"}
        elif term.is_json:
            context[term.name] = {"@id": term.iri, "@type": "@json"}
        else:
            context[term.name] = term.iri
    return context


def _expand_iri(compact: str, base_url: str) -> str:
    if "://" in compact:
        return compact
    prefix, _, local = compact.partition(":")
    if prefix == "lab":
        return f"{terms_namespace(base_url)}{local}"
    return f"{_VOCABULARY_PREFIXES[prefix]}{local}"


def build_terms_document(base_url: str) -> dict[str, object]:
    """JSON-LD vocabulary document served at ``GET /terms``."""
    normalized = base_url.rstrip("/")
    nodes: list[dict[str, object]] = []
    for term in TERMS:
        node: dict[str, object] = {
            "@id": _expand_iri(term.iri, normalized),
            "@type": _RDF_TYPE_BY_TERM_KIND[term.kind],
            "label": term.name,
            "comment": term.definition,
        }
        alias_of = term.deprecated_alias_of or term.compatibility_alias_of
        if alias_of is not None:
            replacement = _TERMS_BY_NAME[alias_of]
            node["@id"] = f"{terms_namespace(normalized)}{term.name}"
            node["equivalentProperty"] = _expand_iri(replacement.iri, normalized)
        elif not term.iri.startswith("lab:"):
            # Standard-vocabulary term: we document the mapping, not the IRI.
            node["seeAlso"] = node["@id"]
            node["@id"] = f"{terms_namespace(normalized)}{term.name}"
        if term.superclass is not None:
            node["subClassOf"] = _expand_iri(term.superclass, normalized)
        if term.domain is not None:
            node["domain"] = _expand_iri(term.domain, normalized)
        if term.range is not None:
            node["range"] = _expand_iri(term.range, normalized)
        if term.emitters:
            node["emitter"] = list(term.emitters)
        if term.in_scheme is not None:
            node["inScheme"] = _expand_iri(term.in_scheme, normalized)
        nodes.append(node)
    return {
        "@context": {
            "rdf": _VOCABULARY_PREFIXES["rdf"],
            "rdfs": _VOCABULARY_PREFIXES["rdfs"],
            "owl": _VOCABULARY_PREFIXES["owl"],
            "skos": _VOCABULARY_PREFIXES["skos"],
            "dcterms": PREFIXES["dcterms"],
            "lab": terms_namespace(normalized),
            "label": "rdfs:label",
            "comment": "rdfs:comment",
            "seeAlso": {"@id": "rdfs:seeAlso", "@type": "@id"},
            "equivalentProperty": {"@id": "owl:equivalentProperty", "@type": "@id"},
            "subClassOf": {"@id": "rdfs:subClassOf", "@type": "@id"},
            "domain": {"@id": "rdfs:domain", "@type": "@id"},
            "range": {"@id": "rdfs:range", "@type": "@id"},
            "inScheme": {"@id": "skos:inScheme", "@type": "@id"},
            "emitter": "dcterms:source",
        },
        "@id": f"{normalized}/terms",
        "@type": "owl:Ontology",
        "label": "Lab Tracker provenance vocabulary",
        "@graph": nodes,
    }


def build_terms_html(base_url: str) -> str:
    """Human-readable vocabulary page served at ``GET /terms``."""
    normalized = base_url.rstrip("/")
    rows: list[str] = []
    for term in TERMS:
        iri = _expand_iri(term.iri, normalized)
        notes: list[str] = []
        if term.deprecated_alias_of is not None:
            notes.append(f"Deprecated; use <code>{term.deprecated_alias_of}</code>.")
        elif term.compatibility_alias_of is not None:
            notes.append(
                f"Compatibility alias of <code>{term.compatibility_alias_of}</code>."
            )
        elif not term.iri.startswith("lab:"):
            notes.append(f"Maps to <code>{term.iri}</code>.")
        if term.superclass is not None:
            notes.append(f"Subclass of <code>{term.superclass}</code>.")
        if term.domain is not None:
            notes.append(f"Domain <code>{term.domain}</code>.")
        if term.range is not None:
            notes.append(f"Range <code>{term.range}</code>.")
        if term.in_scheme is not None:
            notes.append(f"In scheme <code>{term.in_scheme}</code>.")
        if term.emitters:
            emitters = ", ".join(f"<code>{emitter}</code>" for emitter in term.emitters)
            notes.append(f"Emitted by {emitters}.")
        rows.append(
            "<tr>"
            f'<td id="{term.name}"><code>{term.name}</code></td>'
            f"<td>{term.kind.value.replace('_', ' ')}</td>"
            f'<td><a href="{iri}">{iri}</a></td>'
            f"<td>{term.definition}</td>"
            f"<td>{' '.join(notes)}</td>"
            "</tr>"
        )
    row_html = "\n".join(rows)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lab Tracker provenance vocabulary</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 60rem;
  padding: 0 1rem; line-height: 1.5; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ccc; padding: 0.4rem 0.6rem; text-align: left; vertical-align: top; }}
th {{ background: #f4f4f4; }}
code {{ background: #f4f4f4; padding: 0.1rem 0.25rem; }}
</style>
</head>
<body>
<h1>Lab Tracker provenance vocabulary</h1>
<p>Terms used in Lab Tracker's PROV-O/JSON-LD provenance documents and
<code>lt export</code> sidecars. Each <code>lab:</code> IRI resolves to its row
in this table; keys that map to standard vocabularies (PROV-O, schema.org,
Dublin Core) are listed with their mapping. Request this page with
<code>Accept: application/ld+json</code> for the machine-readable form.</p>
<table>
<thead><tr><th>Term</th><th>Kind</th><th>IRI</th><th>Definition</th><th>Notes</th></tr></thead>
<tbody>
{row_html}
</tbody>
</table>
</body>
</html>
"""
