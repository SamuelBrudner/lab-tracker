"""What a person says a capture or session was intended to accomplish."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

WorkScope = Literal["note", "session"]
IntentKind = Literal["question", "exploratory", "calibration", "maintenance"]
IntentSource = Literal["declared", "confirmed_in_review"]


class WorkIntentChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: IntentKind
    question_id: UUID | None = None

    @model_validator(mode="after")
    def check_question(self) -> WorkIntentChoice:
        if (self.kind == "question") != (self.question_id is not None):
            raise ValueError("Choose a question only for work intended to address it.")
        return self


class WorkIntent(BaseModel):
    scope: WorkScope
    work_id: UUID
    kind: IntentKind
    question_id: UUID | None = None
    question_text: str | None = None
    recorded_by_user_id: UUID | None = None
    recorded_at: datetime
    source: IntentSource
    review_draft_id: UUID | None = None
