"""The vocabulary registry stays consistent and /terms dereferences it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lab_tracker.models import (
    AnalysisStatus,
    ClaimRelation,
    ClaimStatus,
    DatasetStatus,
    EntityOrigin,
    ExplorationNodeStatus,
    ExplorationNodeType,
    GoalLinkStatus,
    GoalRelation,
    GoalStatus,
    GoalType,
    NoteStatus,
    OutcomeStatus,
    QuestionLinkRole,
    QuestionStatus,
    QuestionType,
    SessionStatus,
    SessionType,
)
from lab_tracker.vocabulary import (
    CONCEPT_SCHEMES,
    PREFIXES,
    TERMS,
    TermKind,
    build_context,
    build_terms_document,
    build_terms_html,
    concept_iri,
    terms_namespace,
)

_BASE = "https://lab.example.org"


def test_every_term_has_a_unique_name_and_a_definition():
    names = [term.name for term in TERMS]
    assert len(names) == len(set(names))
    for term in TERMS:
        assert term.definition.strip(), term.name
        prefix = term.iri.partition(":")[0]
        assert prefix == "lab" or prefix in PREFIXES, term.name


def test_deprecated_aliases_point_at_existing_terms_with_matching_iris():
    by_name = {term.name: term for term in TERMS}
    aliases = [
        term
        for term in TERMS
        if term.deprecated_alias_of is not None or term.compatibility_alias_of is not None
    ]
    deprecated = [term for term in aliases if term.deprecated_alias_of is not None]
    assert deprecated, "expected at least the sha256/sizeBytes aliases"
    assert any(term.name == "crossLayerBindings" for term in aliases)
    for term in aliases:
        winner_name = term.deprecated_alias_of or term.compatibility_alias_of
        assert winner_name is not None
        winner = by_name[winner_name]
        assert winner.deprecated_alias_of is None
        assert winner.compatibility_alias_of is None
        assert term.kind == winner.kind == TermKind.PROPERTY
        assert term.iri == winner.iri, term.name


def test_context_is_generated_from_the_registry():
    context = build_context(_BASE)
    assert context["lab"] == f"{_BASE}/terms#"
    for prefix, iri in PREFIXES.items():
        assert context[prefix] == iri
    term_names = {term.name for term in TERMS if term.kind == TermKind.PROPERTY}
    context_terms = set(context) - set(PREFIXES) - {"lab"}
    assert context_terms == term_names
    assert context["classifiedAs"] == {"@id": "dcterms:type", "@type": "@id"}
    assert context["crossLayerBinding"] == {
        "@id": "lab:crossLayerBinding",
        "@type": "@id",
    }
    assert context["crossLayerBindings"] == "lab:crossLayerBinding"


def test_terms_document_defines_every_term():
    document = build_terms_document(_BASE)
    assert document["@id"] == f"{_BASE}/terms"
    assert document["@type"] == "owl:Ontology"
    nodes = {node["label"]: node for node in document["@graph"]}
    rdf_types = {
        TermKind.CLASS: "rdfs:Class",
        TermKind.PROPERTY: "rdf:Property",
        TermKind.CONCEPT: "skos:Concept",
        TermKind.CONCEPT_SCHEME: "skos:ConceptScheme",
    }
    for term in TERMS:
        node = nodes[term.name]
        assert node["comment"] == term.definition
        assert node["@type"] == rdf_types[term.kind]
        assert isinstance(node["@id"], str) and node["@id"].startswith("http")


def test_terms_document_carries_class_property_and_concept_metadata():
    document = build_terms_document(_BASE)
    nodes = {node["label"]: node for node in document["@graph"]}
    namespace = terms_namespace(_BASE)

    dataset_class = nodes["Dataset"]
    assert dataset_class["subClassOf"] == f"{PREFIXES['prov']}Entity"
    assert dataset_class["emitter"] == [
        "build_dataset_provenance_document",
        "_dataset_summary_node",
    ]

    goal_type_property = nodes["goalType"]
    assert goal_type_property["domain"] == f"{namespace}Goal"
    assert goal_type_property["range"] == "http://www.w3.org/2000/01/rdf-schema#Literal"
    assert goal_type_property["emitter"] == ["_goal_node"]

    scheme = nodes["scheme/goalType"]
    concept = nodes["goalType/paper"]
    assert scheme["@id"] == f"{namespace}scheme/goalType"
    assert concept["@id"] == f"{namespace}goalType/paper"
    assert concept["inScheme"] == scheme["@id"]


def test_every_lab_iri_in_the_context_resolves_to_a_terms_fragment():
    context = build_context(_BASE)
    namespace = terms_namespace(_BASE)
    document = build_terms_document(_BASE)
    defined_ids = {node["@id"] for node in document["@graph"]}
    for name, value in context.items():
        if name in PREFIXES or name == "lab":
            continue
        compact = value["@id"] if isinstance(value, dict) else value
        if not compact.startswith("lab:"):
            continue
        expanded = f"{namespace}{compact.removeprefix('lab:')}"
        assert expanded in defined_ids, name


def test_custom_metadata_references_resolve_within_the_registry():
    defined = {term.iri for term in TERMS if term.iri.startswith("lab:")}
    for term in TERMS:
        references = (term.superclass, term.domain, term.range, term.in_scheme)
        for reference in references:
            if reference is not None and reference.startswith("lab:"):
                assert reference in defined, (term.name, reference)


_CONCEPT_ENUMS = {
    "questionType": QuestionType,
    "questionStatus": QuestionStatus,
    "datasetStatus": DatasetStatus,
    "analysisStatus": AnalysisStatus,
    "claimStatus": ClaimStatus,
    "noteStatus": NoteStatus,
    "sessionType": SessionType,
    "sessionStatus": SessionStatus,
    "explorationNodeType": ExplorationNodeType,
    "explorationNodeStatus": ExplorationNodeStatus,
    "goalType": GoalType,
    "goalStatus": GoalStatus,
    "entityOrigin": EntityOrigin,
    "claimRelation": ClaimRelation,
    "questionLinkRole": QuestionLinkRole,
    "outcomeStatus": OutcomeStatus,
    "goalRelation": GoalRelation,
    "goalLinkStatus": GoalLinkStatus,
}


def test_concept_schemes_cover_every_domain_enum_value():
    schemes = {scheme.name: scheme for scheme in CONCEPT_SCHEMES}
    assert set(schemes) == set(_CONCEPT_ENUMS)
    for name, enum_type in _CONCEPT_ENUMS.items():
        assert set(schemes[name].values) == {member.value for member in enum_type}, name
        for value in schemes[name].values:
            assert concept_iri(name, value) == f"lab:{name}/{value}"


@pytest.mark.parametrize(
    ("scheme", "value"),
    [("notAScheme", "value"), ("goalType", "not_a_goal")],
)
def test_concept_iri_rejects_unregistered_values(scheme: str, value: str):
    with pytest.raises(ValueError):
        concept_iri(scheme, value)


def test_terms_html_lists_every_term():
    html = build_terms_html(_BASE)
    for term in TERMS:
        assert f"<code>{term.name}</code>" in html


def test_terms_route_is_public_and_content_negotiated(client: TestClient):
    html_response = client.get("/terms")
    assert html_response.status_code == 200
    assert html_response.headers["content-type"].startswith("text/html")
    assert "falsificationCriteria" in html_response.text

    jsonld_response = client.get(
        "/terms",
        headers={"Accept": "application/ld+json"},
    )
    assert jsonld_response.status_code == 200
    assert jsonld_response.headers["content-type"].startswith("application/ld+json")
    document = jsonld_response.json()
    labels = {node["label"] for node in document["@graph"]}
    assert "falsificationCriteria" in labels
