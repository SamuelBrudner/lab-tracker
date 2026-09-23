"""Graph-draft scheduling and context load only the notes they can use.

Review findings L101, L102 and L104: the batch reservation, the scheduler
tick and note-scoped capture bundling used to load every note of the project
and filter in Python.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest
from api_helpers import repository_backed_api

from lab_tracker.auth import AuthContext, Role
from lab_tracker.models import GraphDraftBatchRunStatus, Note, NoteStatus


def _actor() -> AuthContext:
    return AuthContext(user_id=UUID(int=1), role=Role.ADMIN)


class _NoteLoads:
    def __init__(self) -> None:
        self.calls = 0
        self.notes: list[Note] = []

    @property
    def note_ids(self) -> set[UUID]:
        return {note.note_id for note in self.notes}


def _record_note_loads(monkeypatch: pytest.MonkeyPatch, api: Any) -> _NoteLoads:
    repository = api.notes.repository
    original = repository.query_notes
    loads = _NoteLoads()

    def query_notes(**kwargs: Any) -> tuple[list[Note], int]:
        notes, total = original(**kwargs)
        loads.calls += 1
        loads.notes.extend(notes)
        return notes, total

    monkeypatch.setattr(repository, "query_notes", query_notes)
    return loads


def _committed_note(api: Any, project_id: UUID, actor: AuthContext) -> Note:
    note = api.create_note(project_id=project_id, raw_content="Already reviewed.", actor=actor)
    return api.update_note(note.note_id, status=NoteStatus.COMMITTED, actor=actor)


class _UnusedDraftClient:
    provider = "fake"
    model = "fake-batch-model"

    def draft_from_batch(self, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("an empty batch window must not call the provider")

    def close(self) -> None:
        return None


def test_batch_reservation_does_not_load_non_staged_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L101."""

    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Batch note loading", actor=actor)
    committed = _committed_note(api, project.project_id, actor)
    # Content-hash provenance proposals after the run scan notes on their own
    # terms; this test is about the batch reservation's note load.
    monkeypatch.setattr(api.graph_drafts.scheduling, "provenance_links", None)
    loads = _record_note_loads(monkeypatch, api)

    run = api.run_graph_draft_batch_for_project(
        project.project_id,
        draft_client=_UnusedDraftClient(),
        actor=actor,
    )

    assert run.status == GraphDraftBatchRunStatus.SKIPPED
    assert loads.calls
    assert committed.note_id not in loads.note_ids


def test_scheduler_tick_does_not_load_non_staged_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L104."""

    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Scheduler note loading", actor=actor)
    committed = _committed_note(api, project.project_id, actor)
    staged = api.create_note(
        project_id=project.project_id,
        raw_content="Waiting for review.",
        actor=actor,
    )
    scheduling = api.graph_drafts.scheduling
    settings = scheduling.get_graph_draft_batch_settings(project.project_id, actor=actor)
    loads = _record_note_loads(monkeypatch, api)

    reviewers = scheduling._scheduled_reviewers_for_settings(
        settings,
        until=datetime.now(timezone.utc),
    )

    assert [reviewer.reviewer_user_id for reviewer in reviewers] == [staged.created_by_user_id]
    assert loads.calls
    assert committed.note_id not in loads.note_ids
    assert staged.note_id in loads.note_ids


class _EmptyPatchDraftClient:
    provider = "fake"
    model = "fake-batch-model"

    def __init__(self) -> None:
        self.batch_note_ids: list[list[str]] = []

    def draft_from_batch(
        self,
        *,
        batch_context: dict[str, Any],
        user_hint: str | None = None,
    ) -> dict[str, Any]:
        self.batch_note_ids.append(
            [item["id"] for item in batch_context["batch_notes"]]
        )
        return {
            "summary": "Nothing to change",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [],
        }

    def close(self) -> None:
        return None


def test_batch_execution_loads_its_source_notes_in_one_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L104: the frozen source-note set is not fetched one note at a time."""

    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Batch execution note loading", actor=actor)
    first = api.create_note(project_id=project.project_id, raw_content="First.", actor=actor)
    second = api.create_note(project_id=project.project_id, raw_content="Second.", actor=actor)
    monkeypatch.setattr(api.graph_drafts.scheduling, "provenance_links", None)
    original_get = api.notes.repository.notes.get
    single_gets: list[UUID] = []

    def get(note_id: UUID) -> Note | None:
        single_gets.append(note_id)
        return original_get(note_id)

    monkeypatch.setattr(api.notes.repository.notes, "get", get)
    draft_client = _EmptyPatchDraftClient()

    run = api.run_graph_draft_batch_for_project(
        project.project_id,
        draft_client=draft_client,
        actor=actor,
    )

    assert run.source_note_ids == [first.note_id, second.note_id]
    assert run.status == GraphDraftBatchRunStatus.READY, run.error_metadata
    assert draft_client.batch_note_ids == [[str(first.note_id), str(second.note_id)]]
    assert single_gets == []


def test_capture_bundle_sources_load_only_the_bundle_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L102: note-scoped drafts filter capture_bundle_id in SQL."""

    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Capture bundle loading", actor=actor)
    image = api.create_note(
        project_id=project.project_id,
        raw_content="Photo of the notebook.",
        metadata={"capture_bundle_id": "bundle-1", "capture_kind": "image"},
        actor=actor,
    )
    voice = api.create_note(
        project_id=project.project_id,
        raw_content="Voice memo.",
        metadata={"capture_bundle_id": "bundle-1", "capture_kind": "voice"},
        actor=actor,
    )
    other_bundle = api.create_note(
        project_id=project.project_id,
        raw_content="Another capture.",
        metadata={"capture_bundle_id": "bundle-2"},
        actor=actor,
    )
    loose = api.create_note(
        project_id=project.project_id,
        raw_content="Unbundled note.",
        actor=actor,
    )
    loads = _record_note_loads(monkeypatch, api)

    sources = api.graph_drafts.generation.context_builder._source_notes_for_capture(image)

    assert [note.note_id for note in sources] == [image.note_id, voice.note_id]
    assert loads.calls == 1
    assert loads.note_ids == {image.note_id, voice.note_id}
    assert other_bundle.note_id not in loads.note_ids
    assert loose.note_id not in loads.note_ids
