"""Ask for intent only when a note explicitly belongs to a session."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel

from lab_tracker.errors import ValidationError
from lab_tracker.graph_draft_triage import clarification_id, unanswered_clarifications
from lab_tracker.models import (
    EntityType,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftMode,
    Note,
    QuestionStatus,
)
from lab_tracker.work_intent import WorkIntentChoice

if TYPE_CHECKING:
    from lab_tracker.repository import LabTrackerRepository

WORK_INTENT_PROMPTS_KEY = "work_intent_prompts"
MAX_QUESTION_CHOICES = 5


class QuestionOption(BaseModel):
    question_id: UUID
    text: str


class SessionIntentPrompt(BaseModel):
    session_id: UUID
    options: list[QuestionOption]

    def parse_answer(
        self, answer: str, *, repository: LabTrackerRepository, project_id: UUID
    ) -> tuple[WorkIntentChoice, str | None]:
        normalized = answer.strip().casefold()
        if normalized == "exploratory":
            return WorkIntentChoice(kind="exploratory"), None
        if normalized == "calibration":
            return WorkIntentChoice(kind="calibration"), None
        if normalized == "maintenance":
            return WorkIntentChoice(kind="maintenance"), None

        number = normalized.removeprefix("question ").removeprefix("q")
        if number.isascii() and number.isdecimal() and len(number) < 3:
            index = int(number) - 1
            if 0 <= index < len(self.options):
                option = self.options[index]
                return WorkIntentChoice(
                    kind="question", question_id=option.question_id
                ), option.text
        snapshot_titles = {
            option.question_id: option.text
            for option in self.options
            if option.text.strip().casefold() == normalized
        }
        matches = []
        if normalized:
            questions, _ = repository.query_questions(project_id=project_id, search=answer.strip())
            matches = [
                q
                for q in questions
                if q.text.strip().casefold() == normalized
                and q.status in {QuestionStatus.ACTIVE, QuestionStatus.STAGED}
            ]
        if len(matches) == 1:
            match = matches[0]
            if snapshot_titles and match.question_id not in snapshot_titles:
                raise ValidationError("That question has changed. Open the full review.")
            return WorkIntentChoice(kind="question", question_id=match.question_id), match.text
        raise ValidationError(
            "Choose a question number or unique exact title, Exploratory, Calibration, "
            "or Maintenance."
        )


def explicitly_linked_sessions(notes: list[Note]) -> dict[UUID, UUID]:
    """Use a single declared session target. Time overlap remains a hint."""
    result = {}
    for note in notes:
        session_ids = {
            ref.entity_id for ref in note.targets if ref.entity_type == EntityType.SESSION
        }
        if len(session_ids) == 1:
            result[note.note_id] = next(iter(session_ids))
    return result


def add_session_intent_prompts(
    repository: LabTrackerRepository, draft: GraphChangeSet, notes: list[Note]
) -> None:
    if draft.review_assignee_user_id is None:
        return
    links = explicitly_linked_sessions(notes)
    if not links:
        return
    note_intents = repository.work_intents.for_work("note", set(links))
    session_ids = {
        session_id for note_id, session_id in links.items()
        if note_id not in note_intents
    }
    if not session_ids:
        return
    repository.lock_graph_draft_batch_reviewer(
        draft.project_id,
        review_assignee_user_id=draft.review_assignee_user_id,
    )
    recorded = repository.work_intents.for_work("session", session_ids)
    waiting, _ = repository.query_graph_change_sets(
        project_id=draft.project_id,
        draft_mode=GraphDraftMode.GRAPH_BATCH.value,
        status=GraphChangeSetStatus.READY.value,
        assigned_to_user_id=draft.review_assignee_user_id,
        include_operations=False,
    )
    already_asked = {
        prompt.session_id
        for existing in waiting
        for question in unanswered_clarifications(existing)
        if (prompt := session_intent_prompt(existing, question)) is not None
    }
    sessions = [
        session
        for session_id in sorted(session_ids - recorded.keys() - already_asked, key=str)
        if (session := repository.sessions.get(session_id)) is not None
        and session.project_id == draft.project_id
    ]
    if not sessions:
        return

    questions, _ = repository.query_questions(
        project_id=draft.project_id,
        status=QuestionStatus.ACTIVE.value,
        limit=MAX_QUESTION_CHOICES,
        updated_first=True,
    )
    options = [QuestionOption(question_id=q.question_id, text=q.text) for q in questions]
    prompts = {}
    for session in sessions:
        prompt_text = (
            "Which question, if any, did you intend to address in the session that started "
            f"{session.started_at:%Y-%m-%d %H:%M UTC} ({session.link_code})?"
        )
        if options:
            choices = "\n".join(
                f"Question {number}. {option.text}"
                for number, option in enumerate(options, start=1)
            )
            prompt_text += "\n\n" + choices
        prompt_text += (
            "\n\nChoose a question by number or exact title, or choose Exploratory, "
            "Calibration, or Maintenance."
        )
        draft.clarification_requests.append(prompt_text)
        prompts[clarification_id(prompt_text)] = SessionIntentPrompt(
            session_id=session.session_id, options=options
        ).model_dump(mode="json")
    draft.review_metadata = {**draft.review_metadata, WORK_INTENT_PROMPTS_KEY: prompts}


def session_intent_prompt(draft: GraphChangeSet, question: str) -> SessionIntentPrompt | None:
    prompts = draft.review_metadata.get(WORK_INTENT_PROMPTS_KEY, {})
    prompt = prompts.get(clarification_id(question)) if isinstance(prompts, dict) else None
    return SessionIntentPrompt.model_validate(prompt) if prompt is not None else None
