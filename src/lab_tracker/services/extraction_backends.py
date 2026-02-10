"""Question extraction backends.

The core API stages :class:`lab_tracker.models.Question` entities from note content via
``LabTrackerAPI.extract_questions_from_note``. This module provides a pluggable backend
interface so the extraction strategy can evolve beyond regex parsing (e.g. LLMs, OCR).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
import os

from lab_tracker.models import Note
from lab_tracker.services.shared import _extract_question_candidates, _note_text_for_extraction
from lab_tracker.services.llm_clients import LLMClient, OpenAIChatCompletionsClient


@dataclass(frozen=True)
class QuestionCandidate:
    """A candidate question extracted from note content."""

    text: str
    confidence: float


class QuestionExtractionBackend(ABC):
    """Extract candidate questions from a note."""

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Stable identifier for the backend implementation."""

    def requires_raw_asset_bytes(self, note: Note) -> bool:
        """Whether the backend needs raw asset bytes to operate.

        The default regex backend only uses note text fields; OCR backends can override.
        """

        return False

    @abstractmethod
    def extract_questions(
        self,
        note: Note,
        *,
        raw_asset_bytes: bytes | None = None,
    ) -> list[QuestionCandidate]:
        """Return de-duplicated candidate questions extracted from the provided note."""


class RegexQuestionExtractionBackend(QuestionExtractionBackend):
    """Regex-based question extraction matching the project's original heuristics."""

    backend_name = "regex"
    confidence = 0.65

    def extract_questions(
        self,
        note: Note,
        *,
        raw_asset_bytes: bytes | None = None,
    ) -> list[QuestionCandidate]:
        text = _note_text_for_extraction(note)
        return [
            QuestionCandidate(text=candidate, confidence=self.confidence)
            for candidate in _extract_question_candidates(text)
        ]


class LLMQuestionExtractionBackend(QuestionExtractionBackend):
    """LLM-based question extraction with a regex fallback.

    This backend is intentionally defensive:
    - If the LLM call fails (network, auth, parsing), fall back to regex heuristics.
    - If the LLM returns malformed output, fall back to regex heuristics.
    """

    backend_name = "llm"

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        fallback_backend: QuestionExtractionBackend | None = None,
        min_confidence: float = 0.3,
        max_candidates: int = 24,
        max_input_chars: int = 24_000,
    ) -> None:
        self._llm = llm_client
        self._fallback = fallback_backend or RegexQuestionExtractionBackend()
        self._min_confidence = float(min_confidence)
        self._max_candidates = int(max_candidates)
        self._max_input_chars = int(max_input_chars)

    def extract_questions(
        self,
        note: Note,
        *,
        raw_asset_bytes: bytes | None = None,
    ) -> list[QuestionCandidate]:
        text = _note_text_for_extraction(note)
        if not text:
            return []
        if self._max_input_chars > 0 and len(text) > self._max_input_chars:
            text = text[: self._max_input_chars]
        try:
            payload = self._llm.chat(
                _build_question_extraction_messages(text),
                temperature=0.2,
                max_tokens=900,
            )
            candidates = _parse_llm_question_candidates(payload)
            filtered = [
                candidate
                for candidate in candidates
                if candidate.confidence >= self._min_confidence and candidate.text
            ]
            filtered = _dedupe_candidates(filtered)
            if self._max_candidates > 0:
                filtered = filtered[: self._max_candidates]
            if filtered:
                return filtered
        except Exception:
            # Fall back to regex parsing for resilience.
            return self._fallback.extract_questions(note, raw_asset_bytes=raw_asset_bytes)
        # If LLM yields nothing useful, fall back to regex candidates.
        return self._fallback.extract_questions(note, raw_asset_bytes=raw_asset_bytes)


def default_question_extraction_backend() -> QuestionExtractionBackend:
    """Return the default extraction backend.

    Uses the LLM backend when an OpenAI API key is configured, falling back to the
    regex backend otherwise.
    """

    if _openai_api_key_configured():
        llm_client = OpenAIChatCompletionsClient(
            model=os.getenv("LAB_TRACKER_OPENAI_CHAT_MODEL", "gpt-4o-mini"),
            base_url=os.getenv("LAB_TRACKER_OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
        return LLMQuestionExtractionBackend(
            llm_client,
            fallback_backend=RegexQuestionExtractionBackend(),
        )
    return RegexQuestionExtractionBackend()


def _openai_api_key_configured() -> bool:
    return bool(
        (os.getenv("OPENAI_API_KEY") or os.getenv("LAB_TRACKER_OPENAI_API_KEY") or "").strip()
    )


def _build_question_extraction_messages(text: str) -> list[dict[str, str]]:
    system = (
        "You extract research question candidates from unstructured lab notes.\n"
        "Return ONLY valid JSON. Do not wrap the JSON in markdown.\n"
        "\n"
        "Rules:\n"
        "- Extract explicit questions (lines ending with '?' or prefixed with 'Q:'/'Question:').\n"
        "- Convert implicit unknowns into questions (e.g., 'We need to figure out X' -> "
        "'What is X?').\n"
        "- Decompose paragraphs into multiple distinct questions when appropriate.\n"
        "- Avoid duplicates. Keep questions concise and standalone.\n"
        "- Do not include answers, action items, or statements that are not questions.\n"
        "\n"
        "Output schema:\n"
        "{\n"
        '  \"questions\": [\n'
        "    {\"text\": \"<question>\", \"confidence\": <float 0.0-1.0>}\n"
        "  ]\n"
        "}\n"
        "\n"
        "Confidence guidance:\n"
        "- 0.90-1.00: explicit question in the text.\n"
        "- 0.60-0.85: strong implicit question.\n"
        "- 0.30-0.55: plausible but vague.\n"
        "- Below 0.30: omit.\n"
    )
    user = f"Extract question candidates from this text:\n\n{text}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _parse_llm_question_candidates(payload: str) -> list[QuestionCandidate]:
    raw_json = _extract_first_json_object(payload)
    if raw_json is None:
        return []
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return []

    questions: object
    if isinstance(parsed, dict):
        questions = parsed.get("questions")
        if questions is None:
            questions = parsed.get("candidates")
    else:
        questions = parsed

    if not isinstance(questions, list):
        return []

    candidates: list[QuestionCandidate] = []
    for item in questions:
        if isinstance(item, str):
            cleaned = _clean_candidate_text(item)
            if cleaned:
                candidates.append(QuestionCandidate(text=cleaned, confidence=0.5))
            continue
        if not isinstance(item, dict):
            continue
        cleaned = _clean_candidate_text(item.get("text") or item.get("question") or "")
        if not cleaned:
            continue
        confidence = _parse_confidence(item.get("confidence"))
        candidates.append(QuestionCandidate(text=cleaned, confidence=confidence))
    return candidates


def _parse_confidence(value: object) -> float:
    try:
        parsed = float(value) if value is not None else 0.5
    except (TypeError, ValueError):
        parsed = 0.5
    # Be forgiving if the model emits 0-100.
    if parsed > 1.0 and parsed <= 100.0:
        parsed = parsed / 100.0
    if parsed < 0.0:
        return 0.0
    if parsed > 1.0:
        return 1.0
    return parsed


def _clean_candidate_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # Strip common surrounding quotes.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    alpha_count = sum(1 for char in text if char.isalpha())
    if alpha_count < 3:
        return ""
    # Don't force a trailing '?' because existing regex extraction returns both styles.
    return text


def _dedupe_candidates(candidates: list[QuestionCandidate]) -> list[QuestionCandidate]:
    seen: dict[str, int] = {}
    deduped: list[QuestionCandidate] = []
    for candidate in candidates:
        key = candidate.text.casefold()
        existing_index = seen.get(key)
        if existing_index is None:
            seen[key] = len(deduped)
            deduped.append(candidate)
            continue
        if candidate.confidence > deduped[existing_index].confidence:
            deduped[existing_index] = QuestionCandidate(
                text=deduped[existing_index].text,
                confidence=candidate.confidence,
            )
    return deduped


def _extract_first_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None
