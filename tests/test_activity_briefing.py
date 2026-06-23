"""Per-person + time-window retrieval for meeting briefings (WS5).

A PI pulls what a trainee did since last week's meeting; a trainee pulls what
they've done since they last presented. Both rely on filtering committed records
by author (`created_by`) and by a caller-supplied time window (`since`/`until`).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from lab_tracker.db_models import (
    AnalysisModel,
    DatasetModel,
    NoteModel,
    VisualizationModel,
)

FAR_PAST = "2000-01-01T00:00:00+00:00"
FAR_FUTURE = "2999-01-01T00:00:00+00:00"


def _me(client: TestClient, headers: dict[str, str]) -> str:
    return client.get("/auth/me", headers=headers).json()["data"]["user_id"]


def _seed_graph(client: TestClient, headers: dict[str, str]) -> dict[str, str]:
    project_id = client.post(
        "/projects", json={"name": "Briefing project"}, headers=headers
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "What did the trainee do this week?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    dataset_response = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "status": "committed",
            "commit_manifest": {
                "external_artifacts": [
                    {
                        "kind": "entity",
                        "source_system": "s3",
                        "uri": "s3://lab-tracker/briefing/manifest.json",
                        "content_hash": "sha256:briefing",
                    }
                ]
            },
        },
        headers=headers,
    )
    assert dataset_response.status_code == 201, dataset_response.text
    dataset_id = dataset_response.json()["data"]["dataset_id"]
    analysis_id = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "method-1",
            "code_version": "v1",
            "status": "committed",
        },
        headers=headers,
    ).json()["data"]["analysis_id"]
    viz_id = client.post(
        "/visualizations",
        json={
            "analysis_id": analysis_id,
            "viz_type": "figure",
            "file_path": "local/path/figure.png",
            "caption": "A plot",
        },
        headers=headers,
    ).json()["data"]["viz_id"]
    note_id = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Bench observation."},
        headers=headers,
    ).json()["data"]["note_id"]
    return {
        "project_id": project_id,
        "dataset_id": dataset_id,
        "analysis_id": analysis_id,
        "viz_id": viz_id,
        "note_id": note_id,
    }


def _stamp_created_at(client: TestClient, model, pk: str, when: datetime) -> None:
    with client.app.state.db_session_factory() as session:
        row = session.get(model, pk)
        row.created_at = when
        if hasattr(row, "started_at"):
            row.started_at = when
        if hasattr(row, "updated_at"):
            row.updated_at = when
        session.commit()


def test_visualizations_filter_by_author(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    ids = _seed_graph(client, admin_auth_headers)
    author = _me(client, admin_auth_headers)

    mine = client.get(
        "/visualizations",
        params={"project_id": ids["project_id"], "created_by": author},
        headers=admin_auth_headers,
    )
    assert mine.status_code == 200
    assert [v["viz_id"] for v in mine.json()["data"]] == [ids["viz_id"]]

    someone_else = client.get(
        "/visualizations",
        params={
            "project_id": ids["project_id"],
            "created_by": "00000000-0000-0000-0000-000000000000",
        },
        headers=admin_auth_headers,
    )
    assert someone_else.status_code == 200
    assert someone_else.json()["data"] == []


def test_notes_and_datasets_filter_by_time_window(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    ids = _seed_graph(client, admin_auth_headers)
    in_window = datetime(2025, 9, 1, 12, 0, tzinfo=timezone.utc)
    _stamp_created_at(client, NoteModel, ids["note_id"], in_window)
    _stamp_created_at(client, DatasetModel, ids["dataset_id"], in_window)

    for path, key in (("/notes", "note_id"), ("/datasets", "dataset_id")):
        before = client.get(
            path,
            params={"project_id": ids["project_id"], "since": FAR_FUTURE},
            headers=admin_auth_headers,
        )
        assert before.status_code == 200
        assert before.json()["data"] == [], path

        within = client.get(
            path,
            params={
                "project_id": ids["project_id"],
                "since": "2025-07-01T00:00:00+00:00",
                "until": "2025-10-01T00:00:00+00:00",
            },
            headers=admin_auth_headers,
        )
        assert within.status_code == 200
        returned = {item[key] for item in within.json()["data"]}
        assert ids[key] in returned, path


def test_progress_review_briefing_is_person_and_window_scoped(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    ids = _seed_graph(client, admin_auth_headers)
    author = _me(client, admin_auth_headers)
    _stamp_created_at(
        client, AnalysisModel, ids["analysis_id"], datetime(2025, 9, 1, tzinfo=timezone.utc)
    )
    _stamp_created_at(
        client,
        VisualizationModel,
        ids["viz_id"],
        datetime(2025, 9, 1, tzinfo=timezone.utc),
    )

    def briefing(since: str) -> dict:
        response = client.post(
            "/assistant/decision-context",
            json={
                "task_kind": "progress_review",
                "query": "What did this person do since the last meeting?",
                "project_id": ids["project_id"],
                "created_by": author,
                "since": since,
            },
            headers=admin_auth_headers,
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]

    within_window = briefing("2025-07-01T00:00:00+00:00")
    analysis_ids = {item["analysis_id"] for item in within_window["analyses"]}
    assert ids["analysis_id"] in analysis_ids

    after_window = briefing(FAR_FUTURE)
    assert after_window["analyses"] == []
    assert after_window["visualizations"] == []
