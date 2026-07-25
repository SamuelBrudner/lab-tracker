"""Single source of truth for the provenance vocabulary.

Every JSON key emitted in a PROV-O/JSON-LD provenance document is declared
here, with the IRI it maps to and a human-readable definition. The registry
generates both the ``@context`` embedded in every document (see
:mod:`lab_tracker.provenance`) and the ``GET /terms`` vocabulary page that
``lab:`` IRIs dereference to — so the context and its documentation cannot
drift apart.

Naming policy: a key maps to a standard vocabulary IRI (PROV-O, schema.org,
Dublin Core) whenever one exists; the ``lab:`` namespace is reserved for
genuinely domain-specific research-record concepts. Deprecated keys stay in
the context as aliases of their replacement's IRI for one release so older
documents remain interpretable, but builders no longer emit them.
"""

from __future__ import annotations

from dataclasses import dataclass

PREFIXES: dict[str, str] = {
    "prov": "http://www.w3.org/ns/prov#",
    "schema": "https://schema.org/",
    "dcterms": "http://purl.org/dc/terms/",
}


@dataclass(frozen=True)
class Term:
    """One vocabulary term: the JSON key, its IRI, and what it means."""

    name: str
    iri: str
    definition: str
    is_id: bool = False
    is_json: bool = False
    deprecated_alias_of: str | None = None


TERMS: tuple[Term, ...] = (
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
        "goalLink",
        "lab:goalLink",
        "A typed link from a goal to another record.",
        is_id=True,
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
    # --- Experiments and acquisition collections ---
    Term(
        "collection",
        "lab:collection",
        "The logical acquisition collection that owns this immutable snapshot.",
        is_id=True,
    ),
    Term(
        "collectionKey",
        "lab:collectionKey",
        "Watch-configured immutable key identifying an acquisition collection.",
    ),
    Term(
        "hadMember",
        "prov:hadMember",
        "A member entity contained by a PROV collection.",
        is_id=True,
    ),
    Term(
        "manifestHash",
        "lab:manifestHash",
        "SHA-256 identity of a canonical acquisition collection manifest.",
    ),
    Term(
        "memberCount",
        "lab:memberCount",
        "Number of files represented by a collection snapshot.",
    ),
    Term(
        "observedAt",
        "lab:observedAt",
        "When the acquisition collection was observed, ISO 8601.",
    ),
    Term(
        "sourceProvider",
        "lab:sourceProvider",
        "Acquisition system or provider that reported the collection.",
    ),
    Term(
        "sourceUri",
        "lab:sourceUri",
        "Location of the collection in its acquisition system.",
        is_id=True,
    ),
    Term(
        "totalSizeBytes",
        "lab:totalSizeBytes",
        "Sum of member sizes represented by a collection snapshot.",
    ),
    Term(
        "archivedAt",
        "lab:archivedAt",
        "When an Experiment became immutable and archived, ISO 8601.",
    ),
    Term(
        "closedAt",
        "lab:closedAt",
        "When an Experiment stopped accepting Session memberships, ISO 8601.",
    ),
    Term(
        "description",
        "lab:description",
        "Human-readable description of an Experiment.",
    ),
    Term(
        "hasDataset",
        "lab:hasDataset",
        "A Dataset grouped by an Experiment.",
        is_id=True,
    ),
    Term(
        "hasSession",
        "lab:hasSession",
        "A Session grouped by an Experiment.",
        is_id=True,
    ),
    Term(
        "name",
        "lab:name",
        "Human-readable Experiment name.",
    ),
    Term(
        "partOfExperiment",
        "lab:partOfExperiment",
        "The Experiment that groups this Session or Dataset.",
        is_id=True,
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
    ),
)

_TERMS_BY_NAME: dict[str, Term] = {term.name: term for term in TERMS}


def terms_namespace(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/terms#"


def build_context(base_url: str) -> dict[str, object]:
    """The ``@context`` embedded in every provenance document."""
    context: dict[str, object] = dict(PREFIXES)
    context["lab"] = terms_namespace(base_url)
    for term in TERMS:
        if term.is_id:
            context[term.name] = {"@id": term.iri, "@type": "@id"}
        elif term.is_json:
            context[term.name] = {"@id": term.iri, "@type": "@json"}
        else:
            context[term.name] = term.iri
    return context


def _expand_iri(compact: str, base_url: str) -> str:
    prefix, _, local = compact.partition(":")
    if prefix == "lab":
        return f"{terms_namespace(base_url)}{local}"
    return f"{PREFIXES[prefix]}{local}"


def build_terms_document(base_url: str) -> dict[str, object]:
    """JSON-LD vocabulary document served at ``GET /terms``."""
    normalized = base_url.rstrip("/")
    nodes: list[dict[str, object]] = []
    for term in TERMS:
        node: dict[str, object] = {
            "@id": _expand_iri(term.iri, normalized),
            "label": term.name,
            "comment": term.definition,
        }
        if term.deprecated_alias_of is not None:
            replacement = _TERMS_BY_NAME[term.deprecated_alias_of]
            node["@id"] = f"{terms_namespace(normalized)}{term.name}"
            node["equivalentProperty"] = _expand_iri(replacement.iri, normalized)
        elif not term.iri.startswith("lab:"):
            # Standard-vocabulary term: we document the mapping, not the IRI.
            node["seeAlso"] = node["@id"]
            node["@id"] = f"{terms_namespace(normalized)}{term.name}"
        nodes.append(node)
    return {
        "@context": {
            "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
            "owl": "http://www.w3.org/2002/07/owl#",
            "label": "rdfs:label",
            "comment": "rdfs:comment",
            "seeAlso": {"@id": "rdfs:seeAlso", "@type": "@id"},
            "equivalentProperty": {"@id": "owl:equivalentProperty", "@type": "@id"},
        },
        "@id": f"{normalized}/terms",
        "@graph": nodes,
    }


def build_terms_html(base_url: str) -> str:
    """Human-readable vocabulary page served at ``GET /terms``."""
    normalized = base_url.rstrip("/")
    rows: list[str] = []
    for term in TERMS:
        iri = _expand_iri(term.iri, normalized)
        notes = ""
        if term.deprecated_alias_of is not None:
            notes = f"Deprecated; use <code>{term.deprecated_alias_of}</code>."
        elif not term.iri.startswith("lab:"):
            notes = f"Maps to <code>{term.iri}</code>."
        rows.append(
            "<tr>"
            f'<td id="{term.name}"><code>{term.name}</code></td>'
            f'<td><a href="{iri}">{iri}</a></td>'
            f"<td>{term.definition}</td>"
            f"<td>{notes}</td>"
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
<thead><tr><th>Key</th><th>IRI</th><th>Definition</th><th>Notes</th></tr></thead>
<tbody>
{row_html}
</tbody>
</table>
</body>
</html>
"""
