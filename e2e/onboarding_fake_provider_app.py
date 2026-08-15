"""Test-only ASGI app with deterministic member-onboarding AI proposals."""

from __future__ import annotations

import json
import os
from typing import Any

from lab_tracker.app import create_app

if os.environ.get("LAB_TRACKER_E2E_FAKE_ONBOARDING_PROVIDER") != "true":
    raise RuntimeError(
        "The deterministic onboarding provider is restricted to explicit E2E runs."
    )


class DeterministicOnboardingDraftClient:
    """Return a valid staged-question proposal without an external provider call."""

    provider = "e2e-fake"
    model = "deterministic-member-alignment-v1"
    timeout_seconds = 1

    def draft_from_note(
        self,
        *,
        graph_context: dict[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        project_id = str(graph_context["project_id"])
        checkpoint_id = str(graph_context["checkpoint_note_id"])
        live_questions = list(graph_context.get("live_questions") or [])
        operations = [
            {
                "client_ref": f"live_question_{index}",
                "op": "create",
                "entity_type": "question",
                "semantic_type": "suggest_new_question",
                "target_entity_id": None,
                "payload_json": json.dumps(
                    {
                        "project_id": project_id,
                        "text": str(question),
                        "question_type": "other",
                        "status": "staged",
                    }
                ),
                "rationale": "The member identified this as a live project question.",
                "confidence": 0.9,
                "source_refs": [{"source_note_ids": [checkpoint_id]}],
            }
            for index, question in enumerate(live_questions[:3])
        ]
        return {
            "summary": "Align the member's live questions with the shared project graph.",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": operations,
        }

    def close(self) -> None:
        return None


app = create_app()
app.state.graph_draft_client_factory = lambda _settings: DeterministicOnboardingDraftClient()
