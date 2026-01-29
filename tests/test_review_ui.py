from uuid import uuid4

from lab_tracker.models import EntityTagSuggestion, ExtractedEntity, Note
from lab_tracker.review_ui import render_extraction_review


def test_render_extraction_review_includes_entities_and_actions():
    note = Note(
        note_id=uuid4(),
        project_id=uuid4(),
        raw_content="raw fallback",
        transcribed_text="Detected OCR text",
        extracted_entities=[
            ExtractedEntity(
                label="Neuron", confidence=0.92, provenance="ocr:model-1"
            ),
        ],
    )

    html = render_extraction_review(note)

    assert "Detected OCR text" in html
    assert "Neuron" in html
    assert "Provenance: ocr:model-1" in html
    assert "Confidence: 92.0%" in html
    assert "data-action=\"accept\"" in html
    assert "data-action=\"reject\"" in html


def test_render_extraction_review_escapes_and_handles_empty_entities():
    note = Note(
        note_id=uuid4(),
        project_id=uuid4(),
        raw_content="<b>raw</b>",
        transcribed_text=None,
        extracted_entities=[],
    )

    html = render_extraction_review(note)

    assert "Raw content" in html
    assert "No extracted entities." in html
    assert "&lt;b&gt;raw&lt;/b&gt;" in html


def test_render_extraction_review_includes_tag_suggestions():
    note = Note(
        note_id=uuid4(),
        project_id=uuid4(),
        raw_content="raw fallback",
        transcribed_text="Detected OCR text",
        extracted_entities=[
            ExtractedEntity(label="Neuron", confidence=0.92, provenance="ocr:model-1"),
        ],
        tag_suggestions=[
            EntityTagSuggestion(
                suggestion_id=uuid4(),
                entity_label="Neuron",
                vocabulary="NIFSTD",
                term_id="NIFSTD:neuron",
                term_label="Neuron",
                confidence=0.88,
                provenance="tagger:v1",
            )
        ],
    )

    html = render_extraction_review(note)

    assert "NIFSTD - Neuron (NIFSTD:neuron)" in html
    assert "data-action=\"accept-tag\"" in html
    assert "data-action=\"reject-tag\"" in html
