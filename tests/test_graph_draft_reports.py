from __future__ import annotations

import base64

from lab_tracker.graph_draft_reports import (
    build_review_report_context,
    render_review_report_html,
)


def test_review_report_renders_llm_summary_links_operations_and_evidence() -> None:
    change_set_id = "11111111-1111-4111-8111-111111111111"
    note_id = "22222222-2222-4222-8222-222222222222"
    context = build_review_report_context(
        app_base_url="http://lab.local",
        change_sets=[
            {
                "change_set_id": change_set_id,
                "source_note_id": note_id,
                "status": "ready",
                "prompt_version": "analysis-graph-draft-v1",
                "model": "gpt-test",
                "provider": "openai",
                "created_at": "2026-05-08T20:00:00Z",
                "operations": [
                    {
                        "operation_id": "33333333-3333-4333-8333-333333333333",
                        "sequence": 1,
                        "op": "create",
                        "entity_type": "claim",
                        "status": "proposed",
                        "rationale": "The latency figure supports a candidate claim.",
                        "confidence": 0.72,
                        "payload": {"statement": "Latency decreased.", "confidence": 70},
                        "source_refs": [{"label": "figure", "quote": "latency down"}],
                    }
                ],
            }
        ],
        source_notes={
            note_id: {
                "raw_content": "# Analysis Results\n\n![Latency](outputs/latency.png)",
                "transcribed_text": "",
                "metadata": {"source": "ci"},
            }
        },
        raw_assets={
            note_id: {
                "filename": "evidence.md",
                "content_type": "text/markdown",
                "content_base64": base64.b64encode(b"n = 12\np = 0.03").decode("ascii"),
            }
        },
    )
    model_report = {
        "title": "Daily Graph Draft Review",
        "executive_summary": "One draft is ready.",
        "review_priorities": ["Check the claim wording."],
        "draft_summaries": [
            {
                "change_set_id": change_set_id,
                "headline": "Latency claim",
                "interpretation": "The evidence suggests a lower latency.",
                "recommended_action": "Open the draft and approve after checking n.",
                "risks": ["Small sample size."],
                "questions_for_reviewer": ["Should confidence be 70 or lower?"],
            }
        ],
    }

    html = render_review_report_html(context=context, model_report=model_report)

    assert "Daily Graph Draft Review" in html
    assert "Latency claim" in html
    assert f"http://lab.local/app/graph-drafts/{change_set_id}" in html
    assert f"http://lab.local/app/notes/{note_id}" in html
    assert "Latency decreased." in html
    assert "latency down" in html
    assert '<img src="outputs/latency.png" alt="Latency">' in html
    assert "n = 12" in html
    assert "Review, approve, or edit in Lab Tracker" in html
