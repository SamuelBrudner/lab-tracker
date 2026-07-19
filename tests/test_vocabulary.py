"""The vocabulary registry stays consistent and /terms dereferences it."""

from __future__ import annotations

from fastapi.testclient import TestClient

from lab_tracker.vocabulary import (
    PREFIXES,
    TERMS,
    build_context,
    build_terms_document,
    build_terms_html,
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
    deprecated = [term for term in TERMS if term.deprecated_alias_of is not None]
    assert deprecated, "expected at least the sha256/sizeBytes aliases"
    for term in deprecated:
        winner = by_name[term.deprecated_alias_of]
        assert winner.deprecated_alias_of is None
        assert term.iri == winner.iri, term.name


def test_context_is_generated_from_the_registry():
    context = build_context(_BASE)
    assert context["lab"] == f"{_BASE}/terms#"
    for prefix, iri in PREFIXES.items():
        assert context[prefix] == iri
    term_names = {term.name for term in TERMS}
    context_terms = set(context) - set(PREFIXES) - {"lab"}
    assert context_terms == term_names


def test_terms_document_defines_every_term():
    document = build_terms_document(_BASE)
    assert document["@id"] == f"{_BASE}/terms"
    nodes = {node["label"]: node for node in document["@graph"]}
    for term in TERMS:
        node = nodes[term.name]
        assert node["comment"] == term.definition
        assert isinstance(node["@id"], str) and node["@id"].startswith("http")


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
