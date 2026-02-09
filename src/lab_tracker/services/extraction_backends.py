"""Question extraction backends.

The core API stages :class:`lab_tracker.models.Question` entities from note content via
``LabTrackerAPI.extract_questions_from_note``. This module provides a pluggable backend
interface so the extraction strategy can evolve beyond regex parsing (e.g. LLMs, OCR).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from lab_tracker.models import Note
from lab_tracker.services.shared import _extract_question_candidates, _note_text_for_extraction


class QuestionExtractionBackend(ABC):
    """Extract candidate question strings from a note."""

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
    ) -> list[str]:
        """Return de-duplicated candidate questions extracted from the provided note."""


class RegexQuestionExtractionBackend(QuestionExtractionBackend):
    """Regex-based question extraction matching the project's original heuristics."""

    backend_name = "regex"

    def extract_questions(
        self,
        note: Note,
        *,
        raw_asset_bytes: bytes | None = None,
    ) -> list[str]:
        text = _note_text_for_extraction(note)
        return _extract_question_candidates(text)

