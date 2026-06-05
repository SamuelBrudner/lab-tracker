"""Goal attribute validation registry."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict

from lab_tracker.models import GoalType


class PaperAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_venue: str | None = None
    doi: str | None = None
    manuscript_stage: str | None = None
    submission_deadline: date | None = None
    figure_slots: list[str] | None = None


class GrantAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    funding_agency: str | None = None
    mechanism: str | None = None
    rfa_number: str | None = None
    specific_aims: list[str] | None = None
    budget_total: float | None = None
    due_date: date | None = None


class TalkAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_name: str | None = None
    venue: str | None = None
    audience: str | None = None
    event_date: date | None = None
    abstract_due: date | None = None


class OtherAttributes(BaseModel):
    model_config = ConfigDict(extra="allow")


GOAL_ATTRIBUTE_MODELS: dict[GoalType, type[BaseModel]] = {
    GoalType.PAPER: PaperAttributes,
    GoalType.GRANT: GrantAttributes,
    GoalType.TALK: TalkAttributes,
    GoalType.OTHER: OtherAttributes,
}


def validate_goal_attributes(
    goal_type: GoalType | str,
    raw: dict[str, Any] | None,
) -> dict[str, Any]:
    resolved_type = goal_type if isinstance(goal_type, GoalType) else GoalType(goal_type)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("Goal attributes must be a JSON object.")
    model = GOAL_ATTRIBUTE_MODELS[resolved_type].model_validate(raw)
    return model.model_dump(mode="json", exclude_none=True)
