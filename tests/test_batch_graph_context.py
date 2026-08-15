"""Tests for the daily-batch graph context builder (lab-tracker-jdy)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from lab_tracker.api import LabTrackerAPI
from lab_tracker.db_models import NoteModel, QuestionModel
from lab_tracker.models import Note, Session, SessionType
from lab_tracker.services.graph_draft_context import (
    _bounded_batch_source_artifacts,
    _capture_placement,
    _graph_batch_context_summary,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


@contextmanager
def _request_api(client: TestClient) -> Iterator[LabTrackerAPI]:
    session = client.app.state.db_session_factory()
    try:
        repository = SQLAlchemyLabTrackerRepository(session)
        api = client.app.state.lab_tracker_api.for_request(repository)
        yield api
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _create_project(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _create_question(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    text: str,
    status: str = "active",
) -> dict[str, str]:
    response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": text,
            "question_type": "descriptive",
            "status": status,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _quick_capture(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    filename: str,
    body: bytes,
    content_type: str,
) -> str:
    response = client.post(
        "/notes/quick-capture",
        data={"project_id": project_id},
        files={"file": (filename, body, content_type)},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["note_id"]


def _load_notes(client: TestClient, note_ids: list[str]) -> list[Note]:
    with _request_api(client) as api:
        return [api.get_note(UUID(note_id)) for note_id in note_ids]


def _set_note_created_at(
    client: TestClient,
    note_id: str,
    created_at: datetime,
) -> None:
    session = client.app.state.db_session_factory()
    try:
        row = session.get(NoteModel, note_id)
        assert row is not None
        row.created_at = created_at
        row.updated_at = created_at
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _set_note_metadata(client: TestClient, note_id: str, metadata: dict[str, str]) -> None:
    session = client.app.state.db_session_factory()
    try:
        row = session.get(NoteModel, note_id)
        assert row is not None
        row.note_metadata = dict(metadata)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _set_question_timestamps(
    client: TestClient,
    question_id: str,
    *,
    created_at: datetime,
    updated_at: datetime,
) -> None:
    session = client.app.state.db_session_factory()
    try:
        row = session.get(QuestionModel, question_id)
        assert row is not None
        row.created_at = created_at
        row.updated_at = updated_at
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _mark_question_superseded(
    client: TestClient,
    question_id: str,
    *,
    superseded_by_question_id: str | None,
    created_at: datetime,
    updated_at: datetime,
) -> None:
    session = client.app.state.db_session_factory()
    try:
        row = session.get(QuestionModel, question_id)
        assert row is not None
        row.status = "superseded"
        row.superseded_by_question_id = superseded_by_question_id
        row.created_at = created_at
        row.updated_at = updated_at
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def test_batch_context_groups_per_project_with_questions_and_recent_neighborhood(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Plume Navigation")
    active_question = _create_question(
        client,
        admin_auth_headers,
        project_id=project_id,
        text="How do plume statistics shape navigation?",
        status="active",
    )
    staged_question = _create_question(
        client,
        admin_auth_headers,
        project_id=project_id,
        text="Are temporal gradients sufficient cue?",
        status="staged",
    )
    older_note_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_id,
        filename="prior.txt",
        body=b"earlier observation",
        content_type="text/plain",
    )
    note_a_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_id,
        filename="rig2-fly12.jpg",
        body=b"fake-image-bytes-1",
        content_type="image/jpeg",
    )
    note_b_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_id,
        filename="rig2-fly13.jpg",
        body=b"fake-image-bytes-2",
        content_type="image/jpeg",
    )

    batch_notes = _load_notes(client, [note_a_id, note_b_id])

    with _request_api(client) as api:
        packet = api.build_batch_graph_context(batch_notes)

    assert packet["mode"] == "graph_batch"
    # No explicit window: day boundaries are derived from the captures so the
    # narrative has real start/end times instead of inventing them.
    captured_at = sorted(note.created_at for note in batch_notes)
    assert packet["batch_window"] == {
        "since": captured_at[0].isoformat(),
        "until": captured_at[-1].isoformat(),
    }
    assert packet["current_user"] is None
    assert packet["truncated_note_count"] == 0
    # Each capture gets a placement hint; with no sessions here it is unplaced.
    placement = packet["capture_placement"]
    assert {entry["note_id"] for entry in placement} == {str(note_a_id), str(note_b_id)}
    assert all(entry["in_session"] is None for entry in placement)
    assert {note["id"] for note in packet["batch_notes"]} == {note_a_id, note_b_id}
    assert {artifact["note_id"] for artifact in packet["source_artifacts"]} == {
        note_a_id,
        note_b_id,
    }
    assert all(artifact["type"] == "image" for artifact in packet["source_artifacts"])

    assert len(packet["projects"]) == 1
    project_block = packet["projects"][0]
    assert project_block["id"] == project_id
    assert project_block["label"] == "Plume Navigation"
    assert set(project_block["note_ids_in_batch"]) == {note_a_id, note_b_id}
    question_ids_in_context = {q["id"] for q in project_block["active_or_staged_questions"]}
    assert question_ids_in_context == {
        active_question["question_id"],
        staged_question["question_id"],
    }
    recent_note_ids = {n["id"] for n in project_block["recent_notes"]}
    assert older_note_id in recent_note_ids
    assert note_a_id not in recent_note_ids
    assert note_b_id not in recent_note_ids

    summary = packet["context_summary"]
    assert summary["counts"]["projects"] == 1
    assert summary["counts"]["batch_notes"] == 2
    assert summary["counts"]["active_or_staged_questions"] == 2
    assert summary["counts"]["recent_notes"] >= 1
    assert summary["source_artifact_counts"] == {"image": 2}
    assert summary["truncated_note_count"] == 0


def test_batch_context_uses_newest_limited_recent_notes(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Bounded Recent Notes")
    prior_note_ids = [
        _quick_capture(
            client,
            admin_auth_headers,
            project_id=project_id,
            filename=f"prior-{index}.txt",
            body=f"prior observation {index}".encode(),
            content_type="text/plain",
        )
        for index in range(12)
    ]
    batch_note_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_id,
        filename="batch.txt",
        body=b"new batch note",
        content_type="text/plain",
    )
    baseline = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, note_id in enumerate([*prior_note_ids, batch_note_id]):
        _set_note_created_at(client, note_id, baseline + timedelta(minutes=index))

    batch_notes = _load_notes(client, [batch_note_id])

    with _request_api(client) as api:
        packet = api.build_batch_graph_context(batch_notes)

    recent_note_ids = [
        note["id"]
        for note in packet["projects"][0]["recent_notes"]
    ]
    assert recent_note_ids == list(reversed(prior_note_ids[-10:]))
    assert batch_note_id not in recent_note_ids


def test_batch_context_selects_questions_by_recent_update(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Updated Questions")
    old_but_updated = _create_question(
        client,
        admin_auth_headers,
        project_id=project_id,
        text="Old created question with the newest update",
        status="active",
    )
    other_questions = [
        _create_question(
            client,
            admin_auth_headers,
            project_id=project_id,
            text=f"Newer created active question {index}",
            status="active",
        )
        for index in range(55)
    ]
    batch_note_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_id,
        filename="batch.txt",
        body=b"new batch note",
        content_type="text/plain",
    )
    baseline = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _set_question_timestamps(
        client,
        old_but_updated["question_id"],
        created_at=baseline,
        updated_at=baseline + timedelta(days=10),
    )
    for index, question in enumerate(other_questions):
        timestamp = baseline + timedelta(days=1, minutes=index)
        _set_question_timestamps(
            client,
            question["question_id"],
            created_at=timestamp,
            updated_at=timestamp,
        )

    batch_notes = _load_notes(client, [batch_note_id])

    with _request_api(client) as api:
        packet = api.build_batch_graph_context(batch_notes)

    context_question_ids = {
        question["id"]
        for question in packet["projects"][0]["active_or_staged_questions"]
    }
    assert old_but_updated["question_id"] in context_question_ids


def test_batch_context_filters_superseded_question_aliases_in_sql(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Superseded Questions")
    replacement = _create_question(
        client,
        admin_auth_headers,
        project_id=project_id,
        text="Replacement active question",
        status="active",
    )
    old_alias = _create_question(
        client,
        admin_auth_headers,
        project_id=project_id,
        text="Old wording for the replacement",
        status="active",
    )
    irrelevant = [
        _create_question(
            client,
            admin_auth_headers,
            project_id=project_id,
            text=f"Irrelevant superseded question {index}",
            status="active",
        )
        for index in range(110)
    ]
    batch_note_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_id,
        filename="batch.txt",
        body=b"new batch note",
        content_type="text/plain",
    )
    baseline = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _set_question_timestamps(
        client,
        replacement["question_id"],
        created_at=baseline,
        updated_at=baseline + timedelta(days=20),
    )
    _mark_question_superseded(
        client,
        old_alias["question_id"],
        superseded_by_question_id=replacement["question_id"],
        created_at=baseline,
        updated_at=baseline,
    )
    for index, question in enumerate(irrelevant):
        timestamp = baseline + timedelta(days=1, minutes=index)
        _mark_question_superseded(
            client,
            question["question_id"],
            superseded_by_question_id=None,
            created_at=timestamp,
            updated_at=timestamp,
        )

    batch_notes = _load_notes(client, [batch_note_id])

    with _request_api(client) as api:
        packet = api.build_batch_graph_context(batch_notes)

    aliases = packet["projects"][0]["known_aliases"]
    assert any(
        alias.get("relationship") == "superseded_alias_for_replacement"
        and alias.get("entity_id") == replacement["question_id"]
        and alias.get("superseded_entity_id") == old_alias["question_id"]
        for alias in aliases
    )


def test_batch_context_spans_multiple_projects_with_independent_blocks(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_a = _create_project(client, admin_auth_headers, "Project A")
    project_b = _create_project(client, admin_auth_headers, "Project B")
    _create_question(
        client, admin_auth_headers, project_id=project_a, text="Question for A"
    )
    _create_question(
        client, admin_auth_headers, project_id=project_b, text="Question for B"
    )
    note_a_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_a,
        filename="a.txt",
        body=b"in A",
        content_type="text/plain",
    )
    note_b_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_b,
        filename="b.txt",
        body=b"in B",
        content_type="text/plain",
    )

    batch_notes = _load_notes(client, [note_a_id, note_b_id])

    with _request_api(client) as api:
        packet = api.build_batch_graph_context(batch_notes)

    blocks_by_project = {block["id"]: block for block in packet["projects"]}
    assert set(blocks_by_project) == {project_a, project_b}
    assert blocks_by_project[project_a]["note_ids_in_batch"] == [note_a_id]
    assert blocks_by_project[project_b]["note_ids_in_batch"] == [note_b_id]
    a_questions = {q["label"] for q in blocks_by_project[project_a]["active_or_staged_questions"]}
    b_questions = {q["label"] for q in blocks_by_project[project_b]["active_or_staged_questions"]}
    assert a_questions == {"Question for A"}
    assert b_questions == {"Question for B"}


def test_batch_context_carries_window_and_actor_metadata(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    from lab_tracker.auth import AuthContext, Role

    project_id = _create_project(client, admin_auth_headers, "Windowed")
    note_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_id,
        filename="snap.jpg",
        body=b"image",
        content_type="image/jpeg",
    )
    batch_notes = _load_notes(client, [note_id])
    until = datetime(2026, 5, 22, 6, 0, tzinfo=timezone.utc)
    since = until - timedelta(days=1)
    actor = AuthContext(
        user_id=UUID("00000000-0000-0000-0000-000000000123"),
        role=Role.ADMIN,
    )

    with _request_api(client) as api:
        packet = api.build_batch_graph_context(
            batch_notes,
            window=(since, until),
            actor=actor,
        )

    assert packet["batch_window"] == {
        "since": since.isoformat(),
        "until": until.isoformat(),
    }
    assert packet["current_user"] == {
        "id": "00000000-0000-0000-0000-000000000123",
        "role": "admin",
    }


def test_batch_context_truncates_overflow_and_reports_count(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch,
):
    from lab_tracker.services import graph_draft_batch_policy

    monkeypatch.setattr(graph_draft_batch_policy, "BATCH_NOTE_LIMIT", 3)

    project_id = _create_project(client, admin_auth_headers, "Truncation")
    note_ids: list[str] = []
    for index in range(5):
        note_ids.append(
            _quick_capture(
                client,
                admin_auth_headers,
                project_id=project_id,
                filename=f"snap-{index}.jpg",
                body=f"bytes-{index}".encode(),
                content_type="image/jpeg",
            )
        )
    batch_notes = _load_notes(client, note_ids)

    with _request_api(client) as api:
        packet = api.build_batch_graph_context(batch_notes)

    assert packet["truncated_note_count"] == 2
    assert len(packet["batch_notes"]) == 3
    assert packet["context_summary"]["counts"]["batch_notes"] == 3
    assert any(
        "batch truncated" in warning
        for warning in packet["context_summary"]["warnings"]
    )


def test_batch_context_handles_empty_batch(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    with _request_api(client) as api:
        packet = api.build_batch_graph_context([])

    assert packet["mode"] == "graph_batch"
    assert packet["batch_notes"] == []
    assert packet["projects"] == []
    assert packet["truncated_note_count"] == 0
    summary = packet["context_summary"]
    assert summary["counts"]["projects"] == 0
    assert summary["counts"]["batch_notes"] == 0
    assert any(
        warning == "no source artifacts were included"
        for warning in summary["warnings"]
    )
    # Sanity-check serializability of the empty packet.
    json.dumps(packet, sort_keys=True, default=str)


def test_batch_context_flags_and_counts_meeting_notes(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Lab Meetings")
    meeting_note_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_id,
        filename="lab-meeting.txt",
        body=b"PI + trainee: list of follow-up questions",
        content_type="text/plain",
    )
    plain_note_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_id,
        filename="bench.txt",
        body=b"bench observation",
        content_type="text/plain",
    )
    _set_note_metadata(client, meeting_note_id, {"note_type": "meeting"})

    batch_notes = _load_notes(client, [meeting_note_id, plain_note_id])
    with _request_api(client) as api:
        packet = api.build_batch_graph_context(batch_notes)

    notes_by_id = {note["id"]: note for note in packet["batch_notes"]}
    assert notes_by_id[meeting_note_id]["is_meeting"] is True
    assert notes_by_id[plain_note_id]["is_meeting"] is False
    artifacts_by_id = {a["note_id"]: a for a in packet["source_artifacts"]}
    assert artifacts_by_id[meeting_note_id]["is_meeting"] is True
    assert packet["context_summary"]["counts"]["meeting_notes"] == 1


def test_capture_placement_matches_session_window_and_passes_bundle() -> None:
    project_id = uuid4()
    session = Session(
        session_id=uuid4(),
        project_id=project_id,
        session_type=SessionType.SCIENTIFIC,
        started_at=datetime(2026, 6, 25, 9, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc),
    )
    inside = Note(
        note_id=uuid4(),
        project_id=project_id,
        raw_content="Rig 2 Fly 12",
        created_at=datetime(2026, 6, 25, 10, 30, tzinfo=timezone.utc),
        metadata={"capture_bundle_id": "bundle-1"},
    )
    outside = Note(
        note_id=uuid4(),
        project_id=project_id,
        raw_content="Rig 2 Fly 13",
        created_at=datetime(2026, 6, 25, 18, 0, tzinfo=timezone.utc),
    )

    placed = _capture_placement(inside, [session])
    assert placed["in_session"] == {
        "id": str(session.session_id),
        "label": (
            f"{session.session_type.value} session "
            f"{session.started_at.date().isoformat()}"
        ),
    }
    assert placed["capture_bundle_id"] == "bundle-1"

    # A capture outside every session window is unplaceable -> a clarification gap.
    unplaced = _capture_placement(outside, [session])
    assert unplaced["in_session"] is None
    assert unplaced["capture_bundle_id"] is None


def test_batch_source_budget_is_fair_deterministic_and_reports_warning() -> None:
    project_id = uuid4()
    baseline = datetime(2026, 8, 13, tzinfo=timezone.utc)
    notes = [
        Note(
            note_id=UUID("00000000-0000-0000-0000-000000000001"),
            project_id=project_id,
            raw_content="",
            transcribed_text="A" * 20,
            created_at=baseline,
        ),
        Note(
            note_id=UUID("00000000-0000-0000-0000-000000000002"),
            project_id=project_id,
            raw_content="",
            transcribed_text="BB",
            created_at=baseline + timedelta(seconds=1),
        ),
        Note(
            note_id=UUID("00000000-0000-0000-0000-000000000003"),
            project_id=project_id,
            raw_content="",
            transcribed_text="C" * 20,
            created_at=baseline + timedelta(seconds=2),
        ),
    ]

    artifacts, included, omitted, truncated_notes = (
        _bounded_batch_source_artifacts(notes, budget_chars=12)
    )

    # Four characters are reserved for each remaining note. The short middle
    # note uses only two, so its two unused characters roll forward to the
    # final note instead of being lost or allowing the first to starve it.
    assert [artifact["transcript_text"] for artifact in artifacts] == [
        "AAAA",
        "BB",
        "CCCCCC",
    ]
    assert included == 12
    assert omitted == 30
    assert truncated_notes == 2

    packet = {
        "source_artifacts": artifacts,
        "source_context_budget_chars": 12,
        "source_context_included_chars": included,
        "source_context_omitted_chars": omitted,
        "source_context_truncated": True,
        "source_context_truncated_note_count": truncated_notes,
        "projects": [],
        "batch_notes": [],
        "truncated_note_count": 0,
    }
    summary = _graph_batch_context_summary(packet)
    assert summary["source_context_omitted_chars"] == 30
    assert summary["source_context_truncated_note_count"] == 2
    assert any(
        "30 character(s) omitted across 2 note(s)" in warning
        for warning in summary["warnings"]
    )


def test_batch_source_budget_counts_existing_raw_preview_omission() -> None:
    note = Note(
        note_id=uuid4(),
        project_id=uuid4(),
        raw_content="x" * 1500,
    )

    artifacts, included, omitted, truncated_notes = (
        _bounded_batch_source_artifacts([note], budget_chars=2000)
    )

    assert included == 1000
    assert omitted == 500
    assert truncated_notes == 1
    assert artifacts[0]["raw_content_preview"] == "x" * 1000
    assert artifacts[0]["raw_content_preview_omitted_chars"] == 500
    assert artifacts[0]["source_text_omitted_chars"] == 500
