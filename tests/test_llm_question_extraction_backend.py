from __future__ import annotations

from uuid import uuid4

import pytest

from lab_tracker.models import Note
from lab_tracker.services.extraction_backends import LLMQuestionExtractionBackend
from lab_tracker.services.llm_clients import LLMClient


class _StubLLM(LLMClient):
    backend_name = "stub"

    def __init__(self, response: str, *, raise_exc: bool = False) -> None:
        self._response = response
        self._raise = raise_exc

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = 900,
    ) -> str:
        if self._raise:
            raise RuntimeError("boom")
        assert messages
        return self._response


def test_llm_question_extraction_backend_parses_json_and_dedupes():
    response = (
        "Here you go:\n"
        "```json\n"
        "{\n"
        '  \"questions\": [\n'
        "    {\"text\": \"What is X?\", \"confidence\": 0.92},\n"
        "    {\"text\": \"What is X?\", \"confidence\": 0.4},\n"
        "    {\"text\": \"How do we measure Y\", \"confidence\": 87},\n"
        "    {\"text\": \"ok\", \"confidence\": 1.0}\n"
        "  ]\n"
        "}\n"
        "```\n"
    )
    backend = LLMQuestionExtractionBackend(_StubLLM(response), min_confidence=0.6)
    note = Note(note_id=uuid4(), project_id=uuid4(), raw_content="not used by stub")

    candidates = backend.extract_questions(note)

    assert [(candidate.text, candidate.confidence) for candidate in candidates] == [
        ("What is X?", pytest.approx(0.92)),
        ("How do we measure Y", pytest.approx(0.87)),
    ]


def test_llm_question_extraction_backend_falls_back_to_regex_on_error():
    backend = LLMQuestionExtractionBackend(_StubLLM("{}", raise_exc=True))
    note = Note(
        note_id=uuid4(),
        project_id=uuid4(),
        raw_content="Question: What is the baseline distribution\nNotes: check controls",
    )

    candidates = backend.extract_questions(note)

    assert [candidate.text for candidate in candidates] == ["What is the baseline distribution"]
