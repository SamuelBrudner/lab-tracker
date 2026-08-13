from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lab_tracker.graph_drafting import _agentic_search_existing_nodes


def test_checked_in_agentic_draft_recall_at_five() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "agentic_draft_recall_v1.json"
    corpus: dict[str, Any] = json.loads(fixture_path.read_text())
    assert corpus["schema"] == "agentic-draft-recall/v1"
    for case in corpus["cases"]:
        matches = _agentic_search_existing_nodes(case["projects"], case["terms"])
        top_ids = [item["id"] for item in matches[:5]]
        for expected_id in case["expected_top_ids"]:
            assert expected_id in top_ids, case["id"]


def test_agentic_draft_prepass_is_deterministic_and_label_boosted() -> None:
    projects = [
        {
            "id": "project",
            "recent_notes": [
                {"id": "metadata", "label": "Control", "metadata": "dopamine"},
                {"id": "label", "label": "Dopamine observation"},
            ],
        }
    ]
    first = _agentic_search_existing_nodes(projects, ["dopamine"])
    second = _agentic_search_existing_nodes(projects, ["dopamine"])
    assert first == second
    assert [item["id"] for item in first] == ["label", "metadata"]
