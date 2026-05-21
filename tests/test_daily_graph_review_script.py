from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "create-daily-graph-review.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("create_daily_graph_review", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_daily_review_script_prefers_stored_review_brief() -> None:
    script = _load_script()
    brief = {
        "title": "Stored brief",
        "executive_summary": "Already generated.",
        "review_priorities": [],
        "draft_summaries": [],
    }

    assert script._stored_review_brief({"review_brief": brief}) == brief
    assert script._stored_review_brief({"review_brief": {}}) is None
