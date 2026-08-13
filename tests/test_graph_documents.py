from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from lab_tracker.graph_documents import (
    GRAPH_DOCUMENT_CHUNK_OVERLAP,
    GRAPH_DOCUMENT_CHUNK_SIZE,
    GRAPH_DOCUMENT_MAX_CHUNKS,
    GraphNodeDocumentRenderer,
    chunk_graph_document,
)


def test_renderer_normalizes_unicode_keys_and_excludes_path_like_semantic_metadata() -> None:
    row = SimpleNamespace(
        dataset_id=uuid4(),
        commit_hash="abc123",
        manifest_metadata={
            "z_description": "Cafe\u0301 response\r\nsecond line",
            "a_owner": "behavior team",
            "secret_path": "/sensitive/location",
            "checksum": "do-not-embed",
            "api_key": "credential-do-not-embed",
        },
        manifest_nwb_metadata={"session_description": "Photometry acquisition"},
        manifest_bids_metadata={"task_name": "reward choice"},
        manifest_external_artifacts=[
            {"owner": "core facility", "uri": "https://private.invalid/raw"}
        ],
        manifest_files=[{"path": "raw/data.nwb", "checksum": "deadbeef"}],
        status="committed",
        updated_at=None,
    )
    document = GraphNodeDocumentRenderer().render("dataset", row)
    assert "Café response\nsecond line" in (document.semantic_text or "")
    assert "a_owner: behavior team" in (document.semantic_text or "")
    assert "/sensitive/location" not in (document.semantic_text or "")
    assert "do-not-embed" not in (document.semantic_text or "")
    assert "credential-do-not-embed" not in (document.semantic_text or "")
    assert "https://private.invalid/raw" not in (document.semantic_text or "")
    assert "raw/data.nwb" in document.content
    assert "deadbeef" in document.content
    searchable = "\n".join(field.value for field in document.lexical_fields)
    for natural_value in (
        "Café response\nsecond line",
        "behavior team",
        "Photometry acquisition",
        "reward choice",
        "core facility",
    ):
        assert natural_value in searchable


def test_paragraph_chunking_caps_at_eight_and_retains_final_tail() -> None:
    paragraphs = [f"paragraph-{index} " + ("x" * 900) for index in range(50)]
    text = "\r\n\r\n".join(paragraphs)
    chunks = chunk_graph_document(text)
    assert len(chunks) == GRAPH_DOCUMENT_MAX_CHUNKS
    assert all(len(chunk.text) <= GRAPH_DOCUMENT_CHUNK_SIZE for chunk in chunks)
    assert chunks[-1].text.endswith(paragraphs[-1])
    assert GRAPH_DOCUMENT_CHUNK_OVERLAP == 400
    assert chunks == chunk_graph_document(text)


def test_nodes_without_descriptive_text_remain_lexical_only() -> None:
    row = SimpleNamespace(
        session_id=uuid4(),
        session_type="",
        status="active",
        primary_question_id=None,
        started_at=None,
        ended_at=None,
        updated_at=None,
    )
    document = GraphNodeDocumentRenderer().render("session", row)
    # The session type supplies a minimal natural description; the ID/link code
    # remain lexical-only and do not leak into semantic text.
    assert "LTS-" not in (document.semantic_text or "")
    assert any(field.name == "link_code" for field in document.lexical_fields)

    analysis = SimpleNamespace(
        analysis_id=uuid4(),
        code_version="abc123",
        method_hash="method-hash",
        environment_hash="environment-hash",
        external_artifacts=[],
        status="completed",
        updated_at=None,
    )
    analysis_document = GraphNodeDocumentRenderer().render("analysis", analysis)
    assert analysis_document.semantic_text is None
    assert {field.name for field in analysis_document.lexical_fields} >= {
        "code_version",
        "method_hash",
        "environment_hash",
    }
