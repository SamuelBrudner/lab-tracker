"""HTML rendering helpers for extraction review UI."""

from __future__ import annotations

from html import escape

from lab_tracker.models import ExtractedEntity, Note


def render_extraction_review(note: Note) -> str:
    """Return a simple HTML review page for OCR text and extracted entities."""
    text, text_label, text_hint = _select_text(note)
    entities_html = _render_entities(note.extracted_entities)

    return "\n".join(
        [
            "<!doctype html>",
            "<html lang=\"en\">",
            "<head>",
            "  <meta charset=\"utf-8\">",
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
            "  <title>Extraction Review</title>",
            "  <style>",
            "    body { font-family: Arial, Helvetica, sans-serif; background: #f7f7f7; }",
            "    .page { max-width: 960px; margin: 24px auto; padding: 0 16px; }",
            "    .card { background: #ffffff; border: 1px solid #e2e2e2; border-radius: 8px; }",
            "    .card + .card { margin-top: 16px; }",
            "    .card-header { padding: 16px; border-bottom: 1px solid #ececec; }",
            "    .card-body { padding: 16px; }",
            "    .label { font-size: 14px; font-weight: 600; color: #2a2a2a; }",
            "    .hint { font-size: 12px; color: #666666; margin-top: 4px; }",
            "    .ocr-text { white-space: pre-wrap; line-height: 1.5; }",
            "    .entity-list { display: grid; gap: 12px; }",
            "    .entity { border: 1px solid #e6e6e6; border-radius: 6px; padding: 12px; }",
            "    .entity-header { display: flex; justify-content: space-between; gap: 12px; }",
            "    .entity-label { font-weight: 600; color: #1a1a1a; }",
            "    .entity-meta { font-size: 13px; color: #4a4a4a; margin-top: 6px; }",
            "    .entity-actions { display: flex; gap: 8px; margin-top: 10px; }",
            "    .btn { border: 1px solid #cccccc; background: #ffffff; padding: 6px 12px;",
            "           border-radius: 4px; cursor: pointer; font-size: 13px; }",
            "    .btn-accept { border-color: #3f8f4a; color: #2f6f39; }",
            "    .btn-reject { border-color: #b24d4d; color: #7f2f2f; }",
            "    .empty { color: #777777; font-size: 14px; }",
            "  </style>",
            "</head>",
            "<body>",
            "  <div class=\"page\">",
            "    <div class=\"card\">",
            "      <div class=\"card-header\">",
            f"        <div class=\"label\">{escape(text_label)}</div>",
            f"        <div class=\"hint\">{escape(text_hint)}</div>",
            "      </div>",
            "      <div class=\"card-body\">",
            f"        <div class=\"ocr-text\">{escape(text)}</div>",
            "      </div>",
            "    </div>",
            "    <div class=\"card\">",
            "      <div class=\"card-header\">",
            "        <div class=\"label\">Extracted entities</div>",
            "        <div class=\"hint\">Review suggestions and accept or reject.</div>",
            "      </div>",
            "      <div class=\"card-body\">",
            f"        {entities_html}",
            "      </div>",
            "    </div>",
            "  </div>",
            "</body>",
            "</html>",
        ]
    )


def _select_text(note: Note) -> tuple[str, str, str]:
    if note.transcribed_text:
        return note.transcribed_text, "OCR text", "Derived from transcription pipeline."
    if note.raw_content:
        return note.raw_content, "Raw content", "OCR pending; showing raw content."
    return "", "OCR text", "No OCR text available."


def _render_entities(entities: list[ExtractedEntity]) -> str:
    if not entities:
        return "<div class=\"empty\">No extracted entities.</div>"

    items = [_render_entity(entity, index) for index, entity in enumerate(entities, start=1)]
    return "<div class=\"entity-list\">" + "".join(items) + "</div>"


def _render_entity(entity: ExtractedEntity, index: int) -> str:
    label = escape(entity.label)
    provenance = escape(entity.provenance)
    confidence = _format_confidence(entity.confidence)
    label_attr = _escape_attr(entity.label)
    provenance_attr = _escape_attr(entity.provenance)
    confidence_attr = _escape_attr(confidence)

    return "".join(
        [
            "<div class=\"entity\" ",
            f"data-entity-index=\"{index}\" ",
            f"data-entity-label=\"{label_attr}\" ",
            f"data-entity-provenance=\"{provenance_attr}\" ",
            f"data-entity-confidence=\"{confidence_attr}\">",
            "  <div class=\"entity-header\">",
            f"    <div class=\"entity-label\">{label}</div>",
            f"    <div class=\"entity-meta\">Confidence: {confidence}</div>",
            "  </div>",
            f"  <div class=\"entity-meta\">Provenance: {provenance}</div>",
            "  <div class=\"entity-actions\">",
            "    <button type=\"button\" class=\"btn btn-accept\" ",
            f"data-action=\"accept\" data-entity-label=\"{label_attr}\" ",
            f"aria-label=\"Accept {label_attr}\">Accept</button>",
            "    <button type=\"button\" class=\"btn btn-reject\" ",
            f"data-action=\"reject\" data-entity-label=\"{label_attr}\" ",
            f"aria-label=\"Reject {label_attr}\">Reject</button>",
            "  </div>",
            "</div>",
        ]
    )


def _format_confidence(confidence: float) -> str:
    if 0 <= confidence <= 1:
        return f"{confidence:.1%}"
    if 1 < confidence <= 100:
        return f"{confidence:.1f}%"
    return f"{confidence:.2f}"


def _escape_attr(value: str) -> str:
    return escape(value, quote=True)
