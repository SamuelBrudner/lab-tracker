"""Tests for the daily-batch graph context builder (lab-tracker-jdy)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.db_models import ClaimModel, ExplorationNodeModel, NoteModel, QuestionModel
from lab_tracker.models import Note, Session, SessionType
from lab_tracker.services.graph_draft_context import (
    _OPEN_PREDICTION_LIMIT,
    _RECENT_CONTEXT_LIMIT,
    ACTIVE_QUESTION_FLOOR,
    CONTEXT_FIELD_CHAR_LIMIT,
    CUE_MATCHED_LIMIT,
    QUESTION_CONTEXT_LIMIT,
    _bounded_batch_source_artifacts,
    _capture_placement,
    _graph_batch_context_summary,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

_SELECTION_REASONS = {"active_floor", "staged_fill", "recent", "alias_match"}
_SELECTED_CONTEXT_LISTS = (
    "active_or_staged_questions",
    "recent_sessions",
    "recent_datasets",
    "recent_notes",
    "recent_analyses",
    "recent_claims",
    "recent_visualizations",
    "recent_goals",
    "exploration_nodes",
    "cue_matched",
    "known_aliases",
)


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
        "30 inline character(s) and 0 uploaded-text byte(s) omitted across 2 note(s)" in warning
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


def test_batch_context_includes_bounded_uploaded_commit_diff(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _create_project(client, admin_auth_headers, "Commit review")
    prefix = "# Git Commit Evidence\n\n## Diff\n\ndiff --git a/a.py b/a.py\n"
    body = (prefix + ("+changed line\n" * 21_000)).encode()
    note_id = _quick_capture(
        client,
        admin_auth_headers,
        project_id=project_id,
        filename="commit.md",
        body=body,
        content_type="text/markdown",
    )

    notes = _load_notes(client, [note_id])
    with _request_api(client) as api:
        packet = api.build_batch_graph_context(notes)

    artifact = packet["source_artifacts"][0]
    assert artifact["type"] == "text"
    assert artifact["raw_asset_text"].startswith(prefix)
    assert "diff --git a/a.py b/a.py" in artifact["raw_asset_text"]
    assert artifact["raw_asset_text_truncated"] is True
    assert artifact["raw_asset_text_included_bytes"] == 256_000
    assert artifact["raw_asset_text_omitted_bytes"] == len(body) - 256_000
    assert packet["source_context_included_chars"] == 256_000
    assert packet["source_context_omitted_bytes"] == len(body) - 256_000
    assert packet["source_context_truncated"] is True
    assert packet["source_context_truncated_note_count"] == 1


def _create_text_note(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    raw_content: str,
) -> str:
    response = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": raw_content},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["note_id"]


def _create_claim(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    statement: str,
    **fields: object,
) -> dict[str, object]:
    response = client.post(
        "/claims",
        json={"project_id": project_id, "statement": statement, "confidence": 50, **fields},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _create_exploration_node(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    node_type: str,
    title: str,
    target_question_id: str,
    rationale: str | None = None,
) -> str:
    # The service requires choice/rationale/alternatives for decisions and
    # hypothesis/failure_mode/lesson for dead ends; pivots also need an
    # invalidation target, so the tests only create the first two kinds.
    required_fields: dict[str, dict[str, object]] = {
        "decision": {
            "choice": "Take this path.",
            "rationale": rationale or "It fits the evidence.",
            "alternatives_considered": ["Do nothing."],
        },
        "dead_end": {
            "hypothesis": "This path would work.",
            "failure_mode": "It did not.",
            "lesson": "Record it so nobody retries it blindly.",
        },
    }
    payload: dict[str, object] = {
        "project_id": project_id,
        "node_type": node_type,
        "title": title,
        "target": {"entity_type": "question", "entity_id": target_question_id},
        **required_fields[node_type],
    }
    if rationale is not None:
        payload["rationale"] = rationale
    response = client.post("/exploration-nodes", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["node_id"]


def _admin_user_id(client: TestClient, headers: dict[str, str]) -> str:
    response = client.get("/auth/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]["user_id"]


def _set_row_timestamps(client: TestClient, model, row_id: str, at: datetime) -> None:
    session = client.app.state.db_session_factory()
    try:
        row = session.get(model, row_id)
        assert row is not None
        row.created_at = at
        row.updated_at = at
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _batch_packet(client: TestClient, note_ids: list[str], **kwargs) -> dict:
    batch_notes = _load_notes(client, note_ids)
    with _request_api(client) as api:
        return api.build_batch_graph_context(batch_notes, **kwargs)


def test_batch_context_active_questions_fill_before_staged(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Active Floor")
    for index in range(60):
        _create_question(
            client, admin_auth_headers, project_id=project_id,
            text=f"Active question {index}", status="active",
        )
    for index in range(5):
        _create_question(
            client, admin_auth_headers, project_id=project_id,
            text=f"Staged question {index}", status="staged",
        )
    note_id = _create_text_note(
        client, admin_auth_headers, project_id=project_id, raw_content="batch capture"
    )

    packet = _batch_packet(client, [note_id])

    questions = packet["projects"][0]["active_or_staged_questions"]
    assert len(questions) == QUESTION_CONTEXT_LIMIT == ACTIVE_QUESTION_FLOOR
    assert all(item["status"] == "active" for item in questions)
    assert all(item["selection_reason"] == "active_floor" for item in questions)


def test_batch_context_staged_questions_fill_remaining_slots(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Staged Fill")
    active = [
        _create_question(
            client, admin_auth_headers, project_id=project_id,
            text=f"Active question {index}", status="active",
        )
        for index in range(10)
    ]
    for index in range(60):
        _create_question(
            client, admin_auth_headers, project_id=project_id,
            text=f"Staged question {index}", status="staged",
        )
    baseline = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, question in enumerate(active):
        _set_question_timestamps(
            client,
            question["question_id"],
            created_at=baseline,
            updated_at=baseline + timedelta(hours=index),
        )
    note_id = _create_text_note(
        client, admin_auth_headers, project_id=project_id, raw_content="batch capture"
    )

    packet = _batch_packet(client, [note_id])

    questions = packet["projects"][0]["active_or_staged_questions"]
    assert len(questions) == QUESTION_CONTEXT_LIMIT
    active_items = questions[:10]
    staged_items = questions[10:]
    assert all(item["status"] == "active" for item in active_items)
    assert all(item["selection_reason"] == "active_floor" for item in active_items)
    assert len(staged_items) == 40
    assert all(item["status"] == "staged" for item in staged_items)
    assert all(item["selection_reason"] == "staged_fill" for item in staged_items)
    active_updates = [item["updated_at"] for item in active_items]
    assert active_updates == sorted(active_updates, reverse=True)
    assert active_items[0]["id"] == active[-1]["question_id"]


def test_batch_context_cue_matches_pull_project_wide_nodes(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Cue Matching")
    other_project_id = _create_project(client, admin_auth_headers, "Other Cue Project")
    old_claim = _create_claim(
        client, admin_auth_headers, project_id=project_id,
        statement="kynurenine depletion abolishes turning",
    )
    newer_claims = [
        _create_claim(
            client, admin_auth_headers, project_id=project_id,
            statement=f"Unrelated newer claim {index}",
        )
        for index in range(11)
    ]
    baseline = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _set_row_timestamps(client, ClaimModel, str(old_claim["claim_id"]), baseline)
    for index, claim in enumerate(newer_claims):
        _set_row_timestamps(
            client, ClaimModel, str(claim["claim_id"]), baseline + timedelta(days=1 + index)
        )
    other_question = _create_question(
        client, admin_auth_headers, project_id=other_project_id,
        text="Does kynurenine matter elsewhere?", status="active",
    )
    note_id = _create_text_note(
        client, admin_auth_headers, project_id=project_id, raw_content="kynurenine assay rig 2"
    )

    packet = _batch_packet(client, [note_id])

    block = packet["projects"][0]
    assert block["cue_terms"] == ["kynurenine", "assay"]
    matched = block["cue_matched"]
    assert len(matched) <= CUE_MATCHED_LIMIT
    keys = [(item["entity_type"], item["id"]) for item in matched]
    assert len(keys) == len(set(keys))
    assert ("claim", old_claim["claim_id"]) in keys
    old_claim_match = next(item for item in matched if item["id"] == old_claim["claim_id"])
    assert old_claim_match["selection_reason"] == "cue_match:kynurenine"
    assert old_claim_match["label"] == "kynurenine depletion abolishes turning"
    assert "kynurenine" in old_claim_match["snippet"].lower()
    assert old_claim["claim_id"] not in {item["id"] for item in block["recent_claims"]}
    assert ("question", other_question["question_id"]) not in keys
    assert packet["context_summary"]["counts"]["cue_matched"] == len(matched)
    assert packet["context_summary"]["cue_term_count"] == 2

    terse_note_id = _create_text_note(
        client, admin_auth_headers, project_id=project_id, raw_content="the rig 2 fly 12 and"
    )
    terse_packet = _batch_packet(client, [terse_note_id])
    terse_block = terse_packet["projects"][0]
    assert terse_block["cue_terms"] == []
    assert terse_block["cue_matched"] == []


def test_batch_context_restores_hypothesis_and_claim_verification_fields(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Restored Fields")
    hypothesis = "H" * 300
    response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Question with a long hypothesis",
            "question_type": "descriptive",
            "status": "active",
            "hypothesis": hypothesis,
        },
        headers=admin_auth_headers,
    )
    assert response.status_code == 201, response.text
    with_hypothesis = response.json()["data"]
    without_hypothesis = _create_question(
        client, admin_auth_headers, project_id=project_id,
        text="Question without a hypothesis", status="active",
    )
    claim = _create_claim(
        client, admin_auth_headers, project_id=project_id,
        statement="Claim with verification fields",
        falsification_criteria="F" * 300,
        verification_plan="V" * 300,
        refuting_outcome="R" * 300,
        answers_question_ids=[with_hypothesis["question_id"]],
    )
    note_id = _create_text_note(
        client, admin_auth_headers, project_id=project_id, raw_content="batch capture"
    )

    packet = _batch_packet(client, [note_id])

    block = packet["projects"][0]
    questions = {item["id"]: item for item in block["active_or_staged_questions"]}
    assert questions[with_hypothesis["question_id"]]["hypothesis"] == (
        hypothesis[:CONTEXT_FIELD_CHAR_LIMIT]
    )
    assert "hypothesis" not in questions[without_hypothesis["question_id"]]
    claims = {item["id"]: item for item in block["recent_claims"]}
    compact_claim = claims[claim["claim_id"]]
    assert compact_claim["falsification_criteria"] == "F" * CONTEXT_FIELD_CHAR_LIMIT
    assert compact_claim["verification_plan"] == "V" * CONTEXT_FIELD_CHAR_LIMIT
    assert compact_claim["refuting_outcome"] == "R" * CONTEXT_FIELD_CHAR_LIMIT
    assert compact_claim["answers_question_ids"] == [with_hypothesis["question_id"]]


def test_batch_context_lists_open_predictions_per_project(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Open Predictions")
    question = _create_question(
        client, admin_auth_headers, project_id=project_id,
        text="Does kynurenine change turning?", status="active",
    )
    predictions = [
        _create_claim(
            client, admin_auth_headers, project_id=project_id,
            statement=f"Prediction {index}", answers_question_ids=[question["question_id"]],
        )
        for index in range(_OPEN_PREDICTION_LIMIT + 1)
    ]
    baseline = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, claim in enumerate(predictions):
        _set_row_timestamps(
            client, ClaimModel, str(claim["claim_id"]), baseline + timedelta(days=index)
        )
    _create_claim(
        client, admin_auth_headers, project_id=project_id, statement="No question attached"
    )
    supported = _create_claim(
        client, admin_auth_headers, project_id=project_id, statement="Already supported",
        answers_question_ids=[question["question_id"]],
    )
    patched = client.patch(
        f"/claims/{supported['claim_id']}",
        json={"status": "rejected", "terminal_reason": "Refuted."},
        headers=admin_auth_headers,
    )
    assert patched.status_code == 200, patched.text
    note_id = _create_text_note(
        client, admin_auth_headers, project_id=project_id, raw_content="batch capture"
    )

    packet = _batch_packet(client, [note_id])

    block = packet["projects"][0]
    listed = [item["id"] for item in block["open_predictions"]]
    assert listed == [claim["claim_id"] for claim in predictions[:_OPEN_PREDICTION_LIMIT]]
    assert all(item["status"] == "proposed" for item in block["open_predictions"])
    assert all(item["effective_status"] == "proposed" for item in block["open_predictions"])
    assert all("falsification_criteria" in item for item in block["open_predictions"])
    assert all("selection_reason" not in item for item in block["open_predictions"])
    assert packet["context_summary"]["counts"]["open_predictions"] == _OPEN_PREDICTION_LIMIT
    assert all("effective_status" in item for item in block["recent_claims"])


def test_batch_context_includes_recent_exploration_nodes(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Exploration Nodes")
    question = _create_question(
        client, admin_auth_headers, project_id=project_id,
        text="Which cue drives turning?", status="active",
    )
    filler_ids = [
        _create_exploration_node(
            client, admin_auth_headers, project_id=project_id, node_type="decision",
            title=f"Filler decision {index}", target_question_id=question["question_id"],
        )
        for index in range(10)
    ]
    rationale = "Because the gradient assay drifted. " * 12
    decision_id = _create_exploration_node(
        client, admin_auth_headers, project_id=project_id, node_type="decision",
        title="Switch to the ramp assay", target_question_id=question["question_id"],
        rationale=rationale,
    )
    dead_end_id = _create_exploration_node(
        client, admin_auth_headers, project_id=project_id, node_type="dead_end",
        title="Static gradient assay", target_question_id=question["question_id"],
        rationale="No turning bias was detectable.",
    )
    baseline = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, node_id in enumerate([*filler_ids, decision_id, dead_end_id]):
        _set_row_timestamps(
            client, ExplorationNodeModel, node_id, baseline + timedelta(hours=index)
        )
    note_id = _create_text_note(
        client, admin_auth_headers, project_id=project_id, raw_content="batch capture"
    )

    packet = _batch_packet(client, [note_id])

    nodes = packet["projects"][0]["exploration_nodes"]
    assert len(nodes) == _RECENT_CONTEXT_LIMIT
    assert [node["id"] for node in nodes[:2]] == [dead_end_id, decision_id]
    assert nodes[0]["node_type"] == "dead_end"
    assert nodes[0]["label"] == "Static gradient assay"
    assert nodes[0]["rationale"] == "No turning bias was detectable."
    assert nodes[1]["node_type"] == "decision"
    assert nodes[1]["rationale"] == rationale[:CONTEXT_FIELD_CHAR_LIMIT]
    assert nodes[1]["target"] == {
        "entity_type": "question",
        "entity_id": question["question_id"],
    }
    assert all(node["selection_reason"] == "recent" for node in nodes)
    created = [node["created_at"] for node in nodes]
    assert created == sorted(created, reverse=True)
    assert not {filler_ids[0], filler_ids[1]} & {node["id"] for node in nodes}
    assert packet["context_summary"]["counts"]["exploration_nodes"] == _RECENT_CONTEXT_LIMIT


def test_batch_context_labels_recent_notes_with_author(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Note Authors")
    admin_id = _admin_user_id(client, admin_auth_headers)
    older_note_id = _create_text_note(
        client, admin_auth_headers, project_id=project_id, raw_content="earlier observation"
    )
    note_id = _create_text_note(
        client, admin_auth_headers, project_id=project_id, raw_content="batch capture"
    )
    admin_actor = AuthContext(user_id=UUID(admin_id), role=Role.ADMIN)
    colleague_actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)

    as_admin = _batch_packet(client, [note_id], actor=admin_actor)
    as_colleague = _batch_packet(client, [note_id], actor=colleague_actor)
    anonymous = _batch_packet(client, [note_id])

    for packet, expected in ((as_admin, True), (as_colleague, False), (anonymous, None)):
        recent_notes = packet["projects"][0]["recent_notes"]
        assert [item["id"] for item in recent_notes] == [older_note_id]
        assert recent_notes[0]["created_by"] == admin_id
        assert recent_notes[0]["captured_by_current_user"] is expected
        assert recent_notes[0]["selection_reason"] == "recent"
    assert as_admin["batch_notes"][0]["created_by"] == admin_id
    assert "captured_by_current_user" not in as_admin["batch_notes"][0]


def _rich_project(client: TestClient, headers: dict[str, str], name: str) -> dict[str, str]:
    project_id = _create_project(client, headers, name)
    active = [
        _create_question(
            client, headers, project_id=project_id,
            text=f"Active kynurenine question {index}", status="active",
        )
        for index in range(2)
    ]
    _create_question(client, headers, project_id=project_id, text="Staged one", status="staged")
    claim = _create_claim(
        client, headers, project_id=project_id,
        statement="kynurenine depletion abolishes turning",
    )
    _create_exploration_node(
        client, headers, project_id=project_id, node_type="dead_end",
        title="Static gradient assay", target_question_id=active[0]["question_id"],
    )
    older_note_id = _create_text_note(
        client, headers, project_id=project_id, raw_content="earlier observation"
    )
    note_id = _create_text_note(
        client, headers, project_id=project_id, raw_content="kynurenine assay rig 2"
    )
    return {
        "project_id": project_id,
        "claim_id": str(claim["claim_id"]),
        "older_note_id": older_note_id,
        "note_id": note_id,
    }


def test_every_batch_context_item_carries_selection_reason(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    fixture = _rich_project(client, admin_auth_headers, "Selection Reasons")

    packet = _batch_packet(client, [fixture["note_id"]])

    block = packet["projects"][0]
    for key in ("active_or_staged_questions", "recent_notes", "recent_claims",
                "exploration_nodes", "cue_matched", "known_aliases"):
        assert block[key], key
    for key in _SELECTED_CONTEXT_LISTS:
        for item in block[key]:
            reason = item["selection_reason"]
            assert reason in _SELECTION_REASONS or reason.startswith("cue_match:"), (key, item)
    for key in ("batch_notes", "capture_placement", "source_artifacts"):
        assert all("selection_reason" not in item for item in packet[key]), key


def test_batch_context_summary_reports_slot_fill_counts(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    fixture = _rich_project(client, admin_auth_headers, "Slot Fill")

    packet = _batch_packet(client, [fixture["note_id"]])

    block = packet["projects"][0]
    summary = packet["context_summary"]
    recent_lists = (
        "recent_sessions", "recent_datasets", "recent_notes", "recent_analyses",
        "recent_claims", "recent_visualizations", "recent_goals", "exploration_nodes",
    )
    assert summary["slot_fill"] == {
        "active_floor": 2,
        "staged_fill": 1,
        "cue_match": len(block["cue_matched"]),
        "recent": sum(len(block[key]) for key in recent_lists),
        "alias_match": len(block["known_aliases"]),
    }
    assert summary["slot_fill"]["cue_match"] >= 1
    assert summary["counts"]["cue_matched"] == len(block["cue_matched"])
    assert summary["counts"]["exploration_nodes"] == 1
    assert summary["cue_term_count"] == len(block["cue_terms"]) == 2


# --- capture clock: client captured_at > adapter observed_at > server created_at ---


def _bench_session(project_id: UUID) -> Session:
    return Session(
        session_id=uuid4(),
        project_id=project_id,
        session_type=SessionType.SCIENTIFIC,
        started_at=datetime(2026, 6, 25, 9, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc),
    )


def _evening_note(project_id: UUID, metadata: dict[str, str]) -> Note:
    # Received by the server in the evening, well outside the 09:00-12:00 session.
    return Note(
        note_id=uuid4(),
        project_id=project_id,
        raw_content="Rig 2 Fly 12",
        created_at=datetime(2026, 6, 25, 18, 0, tzinfo=timezone.utc),
        metadata=metadata,
    )


def test_capture_placement_prefers_client_captured_at_over_created_at() -> None:
    project_id = uuid4()
    session = _bench_session(project_id)
    note = _evening_note(project_id, {"captured_at": "2026-06-25T10:30:00Z"})

    placed = _capture_placement(note, [session])

    # The phone composed the capture mid-session; the late upload is not the
    # capture time, so the session match follows the client clock.
    assert placed["in_session"] == {
        "id": str(session.session_id),
        "label": f"{session.session_type.value} session 2026-06-25",
    }
    assert placed["observed_at"] == "2026-06-25T10:30:00+00:00"
    assert placed["observed_at_source"] == "client"
    assert placed["created_at"] == note.created_at.isoformat()


def test_capture_placement_uses_adapter_observed_at_and_labels_source() -> None:
    project_id = uuid4()
    session = _bench_session(project_id)
    note = _evening_note(
        project_id, {"evidence_source_observed_at": "2026-06-25T11:15:00+00:00"}
    )

    placed = _capture_placement(note, [session])

    assert placed["in_session"] is not None
    assert placed["observed_at"] == "2026-06-25T11:15:00+00:00"
    assert placed["observed_at_source"] == "adapter"


def test_capture_placement_ignores_unparsable_and_clamps_future_clock() -> None:
    project_id = uuid4()
    session = _bench_session(project_id)

    unparsable = _capture_placement(
        _evening_note(project_id, {"captured_at": "not-a-date"}), [session]
    )
    assert unparsable["in_session"] is None
    assert unparsable["observed_at_source"] == "server"
    assert unparsable["observed_at"] == unparsable["created_at"]

    skewed = _capture_placement(
        _evening_note(project_id, {"captured_at": "2026-06-25T23:59:00Z"}), [session]
    )
    # A clock ahead of the server receipt is clamped to created_at but still
    # reported as the client's clock, so the skew stays visible.
    assert skewed["observed_at"] == skewed["created_at"]
    assert skewed["observed_at_source"] == "client"


def test_batch_context_orders_and_windows_batch_by_observed_at(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Capture clock")
    received_first = datetime(2026, 6, 25, 18, 0, tzinfo=timezone.utc)
    received_second = received_first + timedelta(minutes=1)
    later_capture = "2026-06-25T10:30:00+00:00"
    earlier_capture = "2026-06-25T09:00:00+00:00"
    note_ids: list[str] = []
    for raw_content, captured_at, received_at in (
        ("Rig 2 Fly 12", later_capture, received_first),
        ("Rig 2 Fly 13", earlier_capture, received_second),
    ):
        response = client.post(
            "/notes",
            json={
                "project_id": project_id,
                "raw_content": raw_content,
                "metadata": {"captured_at": captured_at},
            },
            headers=admin_auth_headers,
        )
        assert response.status_code == 201, response.text
        note_id = response.json()["data"]["note_id"]
        _set_note_created_at(client, note_id, received_at)
        note_ids.append(note_id)
    batch_notes = _load_notes(client, note_ids)

    with _request_api(client) as api:
        packet = api.build_batch_graph_context(batch_notes)

    # The model sees the day in capture-clock order, not upload order, and the
    # derived day boundaries follow the same clock.
    assert [note["id"] for note in packet["batch_notes"]] == [note_ids[1], note_ids[0]]
    assert packet["batch_window"] == {"since": earlier_capture, "until": later_capture}
    assert [entry["note_id"] for entry in packet["capture_placement"]] == [
        note["id"] for note in packet["batch_notes"]
    ]
    assert [entry["observed_at_source"] for entry in packet["capture_placement"]] == [
        "client",
        "client",
    ]
