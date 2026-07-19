"""The committed example sidecar stays true to the builder and valid JSON-LD."""

from __future__ import annotations

from provenance_example_fixture import (
    BASE_URL,
    EXAMPLE_PATH,
    build_example_document,
    render_example,
)

_PROV = "http://www.w3.org/ns/prov#"
_SCHEMA = "https://schema.org/"
_DCTERMS = "http://purl.org/dc/terms/"


def test_committed_example_matches_the_builder_output():
    assert EXAMPLE_PATH.exists(), (
        "docs/examples/dataset.prov.jsonld is missing; regenerate with "
        "`uv run python tests/provenance_example_fixture.py`"
    )
    assert EXAMPLE_PATH.read_text(encoding="utf-8") == render_example(), (
        "docs/examples/dataset.prov.jsonld drifted from the provenance builder; "
        "regenerate with `uv run python tests/provenance_example_fixture.py` "
        "and review the diff"
    )


def test_example_expands_cleanly_with_a_json_ld_processor():
    from pyld import jsonld

    expanded = jsonld.expand(build_example_document())
    assert expanded

    by_id = {node["@id"]: node for node in expanded}
    dataset_id = f"{BASE_URL}/datasets/6fce1866-5432-4b70-b582-3f342a9f4f13"
    dataset_node = by_id[dataset_id]

    # Bare keys expand to the standard PROV-O IRIs...
    assert f"{_PROV}wasGeneratedBy" in dataset_node
    assert f"{_PROV}wasAttributedTo" in dataset_node
    # ...file description expands into schema.org...
    file_nodes = [node for node in expanded if "/provenance/files/" in node["@id"]]
    assert file_nodes
    assert all(f"{_SCHEMA}contentSize" in node for node in file_nodes)
    # ...and domain terms land in the dereferenceable /terms# namespace.
    assert f"{BASE_URL}/terms#origin" in dataset_node
