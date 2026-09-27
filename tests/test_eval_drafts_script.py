from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

from lab_tracker.graph_drafting import BATCH_PROMPT_VERSION, GraphDraftingError

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "eval-drafts.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("eval_drafts", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scripted_provider_scores_perfectly(capsys) -> None:
    script = _load_script()

    exit_code = script.main(["--provider", "scripted", "--json"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["provider"] == "golden-day"
    assert payload["model"] == "scripted-v1"
    assert payload["prompt_version"] == BATCH_PROMPT_VERSION
    assert payload["mean_link_precision"] == 1.0
    assert payload["mean_link_recall"] == 1.0
    assert payload["mean_duplicate_create_rate"] == 0.0
    assert len(payload["runs"]) == 1
    assert payload["runs"][0]["operation_count"] > 0


def test_repeat_runs_are_independent_and_averaged(capsys) -> None:
    script = _load_script()

    assert script.main(["--repeat", "2", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["runs"]) == 2
    assert payload["mean_clarification_rate"] == payload["runs"][0]["clarification_rate"]


def test_live_provider_without_configuration_fails_loud(monkeypatch, capsys) -> None:
    script = _load_script()

    def unavailable(_settings):
        raise GraphDraftingError("Unknown graph_draft_provider 'nope'.")

    monkeypatch.setattr(script, "make_graph_draft_client", unavailable)

    exit_code = script.main(["--provider", "live", "--json"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Could not build the live draft client" in captured.err
    assert "Unknown graph_draft_provider" in captured.err
    assert "Traceback" not in captured.err
