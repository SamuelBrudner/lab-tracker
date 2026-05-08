"""HTML review reports for accumulated graph drafts."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import html
import re
from typing import Any


def build_review_report_context(
    *,
    change_sets: list[dict[str, Any]],
    source_notes: dict[str, dict[str, Any]],
    raw_assets: dict[str, dict[str, Any]],
    app_base_url: str,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "app_base_url": app_base_url.rstrip("/"),
        "draft_count": len(change_sets),
        "drafts": [
            _draft_context(change_set, source_notes=source_notes, raw_assets=raw_assets)
            for change_set in change_sets
        ],
    }


def render_review_report_html(
    *,
    context: dict[str, Any],
    model_report: dict[str, Any] | None = None,
) -> str:
    title = str(model_report.get("title") if model_report else "") or "Lab Tracker Review"
    generated_at = str(context.get("generated_at") or "")
    drafts = list(context.get("drafts") or [])
    model_summaries = _model_summary_by_change_set(model_report)
    app_base_url = str(context.get("app_base_url") or "").rstrip("/")
    body = "\n".join(
        [
            _summary_section(model_report=model_report, draft_count=len(drafts)),
            "".join(
                _draft_section(
                    draft,
                    model_summary=model_summaries.get(str(draft.get("change_set_id"))),
                    app_base_url=app_base_url,
                )
                for draft in drafts
            ),
        ]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17201c;
      --muted: #5d6862;
      --line: #d8dfda;
      --paper: #f7f8f5;
      --panel: #ffffff;
      --accent: #285c4d;
      --warn: #8d4f14;
      --bad: #8f2f2f;
    }}
    body {{
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font: 15px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 32px 20px 56px;
    }}
    h1, h2, h3 {{
      line-height: 1.2;
      margin: 0 0 12px;
    }}
    h1 {{
      font-size: 30px;
    }}
    h2 {{
      font-size: 22px;
    }}
    h3 {{
      font-size: 17px;
    }}
    a {{
      color: var(--accent);
      font-weight: 650;
    }}
    .deck {{
      display: grid;
      gap: 16px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
    }}
    .draft {{
      margin-top: 18px;
    }}
    .meta, .chips, .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }}
    .chip {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 9px;
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }}
    .status-ready {{
      color: var(--accent);
      border-color: #8fb9aa;
      background: #edf7f2;
    }}
    .status-failed, .status-rejected {{
      color: var(--bad);
      border-color: #dca3a3;
      background: #fff1f1;
    }}
    .status-accepted {{
      color: var(--accent);
      border-color: #8fb9aa;
    }}
    .action {{
      display: inline-block;
      border: 1px solid var(--accent);
      border-radius: 6px;
      padding: 7px 10px;
      text-decoration: none;
    }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(280px, 0.62fr);
      gap: 16px;
      margin-top: 14px;
    }}
    .operation {{
      border-top: 1px solid var(--line);
      padding-top: 14px;
      margin-top: 14px;
    }}
    pre {{
      overflow: auto;
      background: #f1f4f0;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 12px;
      white-space: pre-wrap;
    }}
    img {{
      max-width: 100%;
      height: auto;
      border: 1px solid var(--line);
      border-radius: 6px;
    }}
    .source {{
      color: var(--muted);
      border-left: 3px solid var(--line);
      padding-left: 10px;
    }}
    .warning {{
      color: var(--warn);
    }}
    @media (max-width: 820px) {{
      .grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <header class="panel">
      <h1>{_escape(title)}</h1>
      <div class="meta">
        <span class="chip">{len(drafts)} draft{'' if len(drafts) == 1 else 's'}</span>
        <span class="chip">Generated {_escape(generated_at)}</span>
      </div>
    </header>
    <div class="deck">
      {body}
    </div>
  </main>
</body>
</html>
"""


def _draft_context(
    change_set: dict[str, Any],
    *,
    source_notes: dict[str, dict[str, Any]],
    raw_assets: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    note_id = str(change_set.get("source_note_id") or "")
    note = source_notes.get(note_id) or {}
    raw = raw_assets.get(note_id) or {}
    operations = list(change_set.get("operations") or [])
    return {
        "change_set_id": change_set.get("change_set_id"),
        "status": change_set.get("status"),
        "prompt_version": change_set.get("prompt_version"),
        "model": change_set.get("model"),
        "provider": change_set.get("provider"),
        "created_at": change_set.get("created_at"),
        "source_note_id": note_id,
        "source_filename": change_set.get("source_filename"),
        "source_content_type": change_set.get("source_content_type"),
        "source_note": {
            "raw_content": note.get("raw_content") or "",
            "transcribed_text": note.get("transcribed_text") or "",
            "metadata": note.get("metadata") or {},
        },
        "raw_asset": _raw_asset_context(raw),
        "operations": [
            {
                "operation_id": operation.get("operation_id"),
                "sequence": operation.get("sequence"),
                "op": operation.get("op"),
                "entity_type": operation.get("entity_type"),
                "status": operation.get("status"),
                "rationale": operation.get("rationale") or "",
                "confidence": operation.get("confidence"),
                "payload": operation.get("payload") or {},
                "source_refs": operation.get("source_refs") or [],
                "error_metadata": operation.get("error_metadata") or {},
            }
            for operation in operations
        ],
    }


def _raw_asset_context(raw: dict[str, Any]) -> dict[str, Any]:
    content_type = str(raw.get("content_type") or "")
    content_base64 = str(raw.get("content_base64") or "")
    if not content_type or not content_base64:
        return {}
    result = {
        "filename": raw.get("filename"),
        "content_type": content_type,
        "size_bytes": raw.get("size_bytes"),
        "checksum": raw.get("checksum"),
    }
    if content_type.startswith("image/"):
        result["image_data_url"] = f"data:{content_type};base64,{content_base64}"
        return result
    if _is_text_content_type(content_type):
        try:
            result["text"] = base64.b64decode(content_base64).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            result["text_error"] = "Raw asset text could not be decoded as UTF-8."
    return result


def _summary_section(*, model_report: dict[str, Any] | None, draft_count: int) -> str:
    if not model_report:
        return (
            '<section class="panel">'
            "<h2>Review Brief</h2>"
            f"<p>{draft_count} graph draft"
            f"{'' if draft_count == 1 else 's'} ready for review.</p>"
            "</section>"
        )
    priorities = "".join(
        f"<li>{_escape(item)}</li>" for item in model_report.get("review_priorities") or []
    )
    priority_html = f"<ul>{priorities}</ul>" if priorities else ""
    return (
        '<section class="panel">'
        "<h2>Review Brief</h2>"
        f"<p>{_escape(model_report.get('executive_summary') or '')}</p>"
        f"{priority_html}"
        "</section>"
    )


def _draft_section(
    draft: dict[str, Any],
    *,
    model_summary: dict[str, Any] | None,
    app_base_url: str,
) -> str:
    change_set_id = str(draft.get("change_set_id") or "")
    source_note_id = str(draft.get("source_note_id") or "")
    headline = (
        str(model_summary.get("headline") if model_summary else "")
        or f"Graph draft {change_set_id}"
    )
    review_url = _join_url(app_base_url, f"/app/graph-drafts/{change_set_id}")
    note_url = _join_url(app_base_url, f"/app/notes/{source_note_id}") if source_note_id else ""
    operations_html = "".join(_operation_section(operation) for operation in draft["operations"])
    status = _escape(str(draft.get("status") or ""))
    prompt_version = _escape(str(draft.get("prompt_version") or ""))
    model = _escape(str(draft.get("model") or ""))
    created_at = _escape(str(draft.get("created_at") or ""))
    return f"""
<section class="panel draft" id="draft-{_escape(change_set_id)}">
  <div class="meta">
    <span class="chip status-{status}">{status}</span>
    <span class="chip">{prompt_version}</span>
    <span class="chip">{model}</span>
    <span class="chip">{created_at}</span>
  </div>
  <h2>{_escape(headline)}</h2>
  {_model_summary_html(model_summary)}
  <div class="actions">
    <a class="action" href="{_escape(review_url)}">Review, approve, or edit in Lab Tracker</a>
    {f'<a class="action" href="{_escape(note_url)}">Open source note</a>' if note_url else ''}
  </div>
  <div class="grid">
    <section>
      <h3>Proposed Operations</h3>
      {operations_html or '<p>No operations proposed.</p>'}
    </section>
    <aside>
      <h3>Evidence And Results</h3>
      {_evidence_html(draft)}
    </aside>
  </div>
</section>
"""


def _model_summary_html(model_summary: dict[str, Any] | None) -> str:
    if not model_summary:
        return ""
    risks = "".join(f"<li>{_escape(item)}</li>" for item in model_summary.get("risks") or [])
    questions = "".join(
        f"<li>{_escape(item)}</li>"
        for item in model_summary.get("questions_for_reviewer") or []
    )
    return (
        f"<p>{_escape(model_summary.get('interpretation') or '')}</p>"
        f"<p><strong>Recommended action:</strong> "
        f"{_escape(model_summary.get('recommended_action') or '')}</p>"
        f"{'<h3>Risks</h3><ul>' + risks + '</ul>' if risks else ''}"
        f"{'<h3>Questions</h3><ul>' + questions + '</ul>' if questions else ''}"
    )


def _operation_section(operation: dict[str, Any]) -> str:
    title = f"{operation.get('op')} {operation.get('entity_type')}"
    status = _escape(str(operation.get("status") or ""))
    source_refs = "".join(
        f'<p class="source">{_escape(_source_ref_text(ref))}</p>'
        for ref in operation.get("source_refs") or []
    )
    error = (operation.get("error_metadata") or {}).get("message")
    return f"""
<article class="operation" id="operation-{_escape(str(operation.get('operation_id') or ''))}">
  <div class="meta">
    <span class="chip status-{status}">{status}</span>
    <span class="chip">{_escape(title)}</span>
    {_confidence_chip(operation.get('confidence'))}
  </div>
  <p>{_escape(operation.get('rationale') or '')}</p>
  {source_refs}
  {f'<p class="warning">{_escape(str(error))}</p>' if error else ''}
  <pre>{_escape(_jsonish(operation.get('payload') or {}))}</pre>
</article>
"""


def _evidence_html(draft: dict[str, Any]) -> str:
    note = draft.get("source_note") or {}
    raw_asset = draft.get("raw_asset") or {}
    chunks: list[str] = []
    if raw_asset.get("image_data_url"):
        chunks.append(
            f'<img src="{_escape(str(raw_asset["image_data_url"]))}" '
            f'alt="{_escape(str(raw_asset.get("filename") or "Source image"))}">'
        )
    raw_content = str(note.get("raw_content") or "").strip()
    if raw_content:
        chunks.append(_markdownish_html(raw_content))
    transcribed = str(note.get("transcribed_text") or "").strip()
    if transcribed:
        chunks.append("<h3>Transcript</h3>" + _markdownish_html(transcribed))
    raw_text = str(raw_asset.get("text") or "").strip()
    if raw_text:
        chunks.append("<h3>Raw Asset Text</h3>" + _markdownish_html(raw_text))
    metadata = note.get("metadata") or {}
    if metadata:
        chunks.append("<h3>Metadata</h3><pre>" + _escape(_jsonish(metadata)) + "</pre>")
    return "\n".join(chunks) or "<p>No source evidence text or image was available.</p>"


def _markdownish_html(value: str) -> str:
    lines = value.splitlines()
    output: list[str] = []
    in_list = False
    in_code = False
    code_lines: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            output.append(f"<p>{_inline_markup(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal in_list
        if in_list:
            output.append("</ul>")
            in_list = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                output.append("<pre>" + _escape("\n".join(code_lines)) + "</pre>")
                code_lines.clear()
                in_code = False
            else:
                flush_paragraph()
                flush_list()
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            flush_paragraph()
            flush_list()
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1)) + 2
            output.append(f"<h{level}>{_inline_markup(heading.group(2))}</h{level}>")
            continue
        item = re.match(r"^[-*]\s+(.+)$", stripped)
        if item:
            flush_paragraph()
            if not in_list:
                output.append("<ul>")
                in_list = True
            output.append(f"<li>{_inline_markup(item.group(1))}</li>")
            continue
        paragraph.append(stripped)

    if in_code:
        output.append("<pre>" + _escape("\n".join(code_lines)) + "</pre>")
    flush_paragraph()
    flush_list()
    return "\n".join(output)


def _inline_markup(value: str) -> str:
    escaped = _escape(value)
    escaped = re.sub(
        r"!\[([^\]]*)\]\(([^)]+)\)",
        lambda match: (
            f'<img src="{_escape(match.group(2))}" alt="{_escape(match.group(1))}">'
        ),
        escaped,
    )
    return re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda match: f'<a href="{_escape(match.group(2))}">{match.group(1)}</a>',
        escaped,
    )


def _model_summary_by_change_set(
    model_report: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if not model_report:
        return {}
    summaries: dict[str, dict[str, Any]] = {}
    for item in model_report.get("draft_summaries") or []:
        if isinstance(item, dict) and item.get("change_set_id"):
            summaries[str(item["change_set_id"])] = item
    return summaries


def _source_ref_text(ref: dict[str, Any]) -> str:
    label = f"{ref.get('label')}: " if ref.get("label") else ""
    quote = str(ref.get("quote") or "")
    return f"{label}{quote}" or "Source reference"


def _confidence_chip(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    return f'<span class="chip">{round(value * 100)}% confidence</span>'


def _is_text_content_type(content_type: str) -> bool:
    normalized = content_type.split(";", 1)[0].strip().lower()
    return normalized.startswith("text/") or normalized in {
        "application/json",
        "application/ld+json",
        "application/markdown",
        "application/x-ndjson",
        "application/xml",
    }


def _join_url(base_url: str, path: str) -> str:
    if not base_url:
        return path
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _jsonish(value: Any) -> str:
    import json

    return json.dumps(value, indent=2, sort_keys=True)


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)
