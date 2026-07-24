"""PostgreSQL coverage for JSON-bearing association filters."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from lab_tracker.db_models import NoteTargetModel
from lab_tracker.models import EntityType

pytestmark = pytest.mark.postgres


def _create_project(
    client: TestClient,
    headers: dict[str, str],
    *,
    label: str,
) -> UUID:
    response = client.post(
        "/projects",
        json={"name": f"{label} project"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["data"]["project_id"])


def _create_question(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: UUID,
    label: str,
) -> UUID:
    response = client.post(
        "/questions",
        json={
            "project_id": str(project_id),
            "text": f"{label} question",
            "question_type": "descriptive",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["data"]["question_id"])


def _create_dataset(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: UUID,
    question_id: UUID,
) -> UUID:
    response = client.post(
        "/datasets",
        json={
            "project_id": str(project_id),
            "primary_question_id": str(question_id),
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["data"]["dataset_id"])


def _create_analysis(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: UUID,
    dataset_ids: list[UUID],
    label: str,
) -> dict[str, object]:
    response = client.post(
        "/analyses",
        json={
            "project_id": str(project_id),
            "dataset_ids": [str(dataset_id) for dataset_id in dataset_ids],
            "method_hash": f"{label}-method",
            "code_version": f"{label}-code",
            "external_artifacts": [
                {
                    "kind": "activity",
                    "source_system": "s3",
                    "uri": f"s3://lab-tracker/{label}/run.json",
                    "content_hash": f"sha256:{label}",
                    "metadata": {
                        "backend": "postgresql",
                        "dimensions": [2, 3],
                        "verified": True,
                    },
                }
            ],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _create_note(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: UUID,
    label: str,
    targets: list[tuple[EntityType, UUID]] | None = None,
) -> dict[str, object]:
    response = client.post(
        "/notes",
        json={
            "project_id": str(project_id),
            "raw_content": f"{label} note",
            "targets": [
                {"entity_type": entity_type.value, "entity_id": str(entity_id)}
                for entity_type, entity_id in targets or []
            ],
            "metadata": {
                "backend": "postgresql",
                "filter_case": label,
            },
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _ids(response: Response, key: str) -> list[str]:
    assert response.status_code == 200, response.text
    return [item[key] for item in response.json()["data"]]


def test_postgres_analysis_association_filters_do_not_compare_json_columns(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
) -> None:
    project_id = _create_project(
        postgres_client,
        postgres_admin_auth_headers,
        label="Analysis JSON filters",
    )
    question_one_id = _create_question(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        label="First",
    )
    question_two_id = _create_question(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        label="Second",
    )
    dataset_one_id = _create_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        question_id=question_one_id,
    )
    dataset_two_id = _create_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        question_id=question_two_id,
    )
    dataset_three_id = _create_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        question_id=question_one_id,
    )

    cross_question_analysis = _create_analysis(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        dataset_ids=[dataset_one_id, dataset_two_id],
        label="cross-question",
    )
    multi_match_analysis = _create_analysis(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        dataset_ids=[dataset_one_id, dataset_three_id],
        label="multi-match",
    )
    expected_ids = {
        str(cross_question_analysis["analysis_id"]),
        str(multi_match_analysis["analysis_id"]),
    }

    by_dataset = postgres_client.get(
        "/analyses",
        params={"project_id": str(project_id), "dataset_id": str(dataset_one_id)},
        headers=postgres_admin_auth_headers,
    )
    by_question = postgres_client.get(
        "/analyses",
        params={"project_id": str(project_id), "question_id": str(question_one_id)},
        headers=postgres_admin_auth_headers,
    )
    matching_same_association = postgres_client.get(
        "/analyses",
        params={
            "project_id": str(project_id),
            "dataset_id": str(dataset_one_id),
            "question_id": str(question_one_id),
        },
        headers=postgres_admin_auth_headers,
    )
    mismatched_cross_association = postgres_client.get(
        "/analyses",
        params={
            "project_id": str(project_id),
            "dataset_id": str(dataset_one_id),
            "question_id": str(question_two_id),
        },
        headers=postgres_admin_auth_headers,
    )
    reverse_mismatch = postgres_client.get(
        "/analyses",
        params={
            "project_id": str(project_id),
            "dataset_id": str(dataset_two_id),
            "question_id": str(question_one_id),
        },
        headers=postgres_admin_auth_headers,
    )
    paged = postgres_client.get(
        "/analyses",
        params={
            "project_id": str(project_id),
            "question_id": str(question_one_id),
            "limit": 1,
            "offset": 1,
        },
        headers=postgres_admin_auth_headers,
    )
    missing = postgres_client.get(
        "/analyses",
        params={
            "project_id": str(project_id),
            "dataset_id": str(uuid4()),
            "question_id": str(question_one_id),
        },
        headers=postgres_admin_auth_headers,
    )

    dataset_ids = _ids(by_dataset, "analysis_id")
    question_ids = _ids(by_question, "analysis_id")
    same_association_ids = _ids(matching_same_association, "analysis_id")
    assert set(dataset_ids) == expected_ids
    assert by_dataset.json()["meta"]["total"] == 2
    assert set(question_ids) == expected_ids
    assert len(question_ids) == 2
    assert by_question.json()["meta"]["total"] == 2
    assert set(same_association_ids) == expected_ids
    assert matching_same_association.json()["meta"]["total"] == 2
    assert _ids(paged, "analysis_id") == question_ids[1:2]
    assert paged.json()["meta"]["total"] == 2
    assert _ids(mismatched_cross_association, "analysis_id") == []
    assert mismatched_cross_association.json()["meta"]["total"] == 0
    assert _ids(reverse_mismatch, "analysis_id") == []
    assert reverse_mismatch.json()["meta"]["total"] == 0
    assert _ids(missing, "analysis_id") == []
    assert missing.json()["meta"]["total"] == 0

    returned_cross_analysis = next(
        item
        for item in by_question.json()["data"]
        if item["analysis_id"] == str(cross_question_analysis["analysis_id"])
    )
    assert (
        returned_cross_analysis["external_artifacts"]
        == cross_question_analysis["external_artifacts"]
    )


def test_postgres_note_target_filters_do_not_compare_json_columns(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
) -> None:
    project_id = _create_project(
        postgres_client,
        postgres_admin_auth_headers,
        label="Note JSON filters",
    )
    question_id = _create_question(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        label="Target",
    )
    other_question_id = _create_question(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        label="Other target",
    )
    dataset_one_id = _create_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        question_id=question_id,
    )
    dataset_two_id = _create_dataset(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        question_id=other_question_id,
    )

    multi_target_note = _create_note(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        label="multi-target",
        targets=[
            (EntityType.DATASET, dataset_one_id),
            (EntityType.DATASET, dataset_two_id),
            (EntityType.QUESTION, question_id),
        ],
    )
    second_dataset_note = _create_note(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        label="second-dataset",
        targets=[(EntityType.DATASET, dataset_one_id)],
    )
    shared_id_note = _create_note(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        label="shared-id",
    )
    shared_target_id = uuid4()
    with postgres_client.app.state.db_session_factory() as session:
        session.add_all(
            [
                NoteTargetModel(
                    note_id=UUID(str(shared_id_note["note_id"])),
                    entity_type=EntityType.CLAIM,
                    entity_id=shared_target_id,
                ),
                NoteTargetModel(
                    note_id=UUID(str(shared_id_note["note_id"])),
                    entity_type=EntityType.VISUALIZATION,
                    entity_id=shared_target_id,
                ),
            ]
        )
        session.commit()

    by_type = postgres_client.get(
        "/notes",
        params={
            "project_id": str(project_id),
            "target_entity_type": EntityType.DATASET.value,
        },
        headers=postgres_admin_auth_headers,
    )
    by_dataset_id = postgres_client.get(
        "/notes",
        params={
            "project_id": str(project_id),
            "target_entity_id": str(dataset_one_id),
        },
        headers=postgres_admin_auth_headers,
    )
    by_duplicate_id = postgres_client.get(
        "/notes",
        params={
            "project_id": str(project_id),
            "target_entity_id": str(shared_target_id),
        },
        headers=postgres_admin_auth_headers,
    )
    matching_same_target = postgres_client.get(
        "/notes",
        params={
            "project_id": str(project_id),
            "target_entity_type": EntityType.QUESTION.value,
            "target_entity_id": str(question_id),
        },
        headers=postgres_admin_auth_headers,
    )
    mismatched_cross_target = postgres_client.get(
        "/notes",
        params={
            "project_id": str(project_id),
            "target_entity_type": EntityType.DATASET.value,
            "target_entity_id": str(question_id),
        },
        headers=postgres_admin_auth_headers,
    )
    paged = postgres_client.get(
        "/notes",
        params={
            "project_id": str(project_id),
            "target_entity_type": EntityType.DATASET.value,
            "limit": 1,
            "offset": 1,
        },
        headers=postgres_admin_auth_headers,
    )
    missing = postgres_client.get(
        "/notes",
        params={
            "project_id": str(project_id),
            "target_entity_id": str(uuid4()),
        },
        headers=postgres_admin_auth_headers,
    )

    expected_dataset_note_ids = {
        str(multi_target_note["note_id"]),
        str(second_dataset_note["note_id"]),
    }
    type_ids = _ids(by_type, "note_id")
    dataset_target_ids = _ids(by_dataset_id, "note_id")
    assert set(type_ids) == expected_dataset_note_ids
    assert len(type_ids) == 2
    assert by_type.json()["meta"]["total"] == 2
    assert set(dataset_target_ids) == expected_dataset_note_ids
    assert by_dataset_id.json()["meta"]["total"] == 2
    assert _ids(by_duplicate_id, "note_id") == [str(shared_id_note["note_id"])]
    assert by_duplicate_id.json()["meta"]["total"] == 1
    assert _ids(matching_same_target, "note_id") == [str(multi_target_note["note_id"])]
    assert matching_same_target.json()["meta"]["total"] == 1
    assert _ids(mismatched_cross_target, "note_id") == []
    assert mismatched_cross_target.json()["meta"]["total"] == 0
    assert _ids(paged, "note_id") == type_ids[1:2]
    assert paged.json()["meta"]["total"] == 2
    assert _ids(missing, "note_id") == []
    assert missing.json()["meta"]["total"] == 0

    returned_multi_target_note = next(
        item
        for item in by_type.json()["data"]
        if item["note_id"] == str(multi_target_note["note_id"])
    )
    assert returned_multi_target_note["metadata"] == multi_target_note["metadata"]
