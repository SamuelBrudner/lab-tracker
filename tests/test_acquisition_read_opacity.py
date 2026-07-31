from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from read_opacity_inventory import (
    ACQUISITION_READ_OPACITY_VARIANTS,
    ACQUISITION_SUITE,
    READ_OPACITY_VARIANTS_BY_ID,
)


@dataclass(frozen=True)
class AcquisitionReadRecords:
    experiment_id: str
    session_id: str
    dataset_id: str
    collection_id: str
    snapshot_id: str
    missing_experiment_id: str
    missing_session_id: str
    missing_dataset_id: str
    missing_collection_id: str
    missing_snapshot_id: str


@dataclass(frozen=True)
class ReadCase:
    name: str
    existing_path: str
    missing_path: str
    not_found_label: str
    authorized_value: Callable[[Any], object]
    expected_value: object


def _create_acquisition_records(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
) -> AcquisitionReadRecords:
    question = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Which acquisition reads stay opaque?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    )
    assert question.status_code == 201, question.text
    question_id = question.json()["data"]["question_id"]

    experiment = client.post(
        "/experiments",
        json={
            "project_id": project_id,
            "name": "Opaque acquisition",
            "primary_question_id": question_id,
        },
        headers=headers,
    )
    assert experiment.status_code == 201, experiment.text
    experiment_id = experiment.json()["data"]["experiment_id"]

    session = client.post(
        "/sessions",
        json={
            "project_id": project_id,
            "session_type": "operational",
        },
        headers=headers,
    )
    assert session.status_code == 201, session.text
    session_id = session.json()["data"]["session_id"]

    dataset = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
        },
        headers=headers,
    )
    assert dataset.status_code == 201, dataset.text
    dataset_id = dataset.json()["data"]["dataset_id"]

    assert (
        client.put(
            f"/experiments/{experiment_id}/sessions/{session_id}",
            headers=headers,
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/experiments/{experiment_id}/datasets/{dataset_id}",
            headers=headers,
        ).status_code
        == 200
    )

    snapshot = client.post(
        f"/sessions/{session_id}/collections/trials/snapshots",
        json={
            "client_capture_id": "opaque-capture",
            "observed_at": "2026-07-24T12:00:00Z",
            "complete": True,
            "manifest": {
                "schema_version": 1,
                "members": [
                    {
                        "path": "trial/data.bin",
                        "checksum": "a" * 64,
                        "size_bytes": 1,
                    }
                ],
            },
        },
        headers=headers,
    )
    assert snapshot.status_code == 201, snapshot.text
    snapshot_id = snapshot.json()["data"]["snapshot_id"]
    collections = client.get(
        f"/sessions/{session_id}/collections",
        headers=headers,
    )
    assert collections.status_code == 200, collections.text
    collection_id = collections.json()["data"][0]["collection_id"]

    return AcquisitionReadRecords(
        experiment_id=experiment_id,
        session_id=session_id,
        dataset_id=dataset_id,
        collection_id=collection_id,
        snapshot_id=snapshot_id,
        missing_experiment_id=str(uuid4()),
        missing_session_id=str(uuid4()),
        missing_dataset_id=str(uuid4()),
        missing_collection_id=str(uuid4()),
        missing_snapshot_id=str(uuid4()),
    )


def _read_cases(records: AcquisitionReadRecords) -> tuple[ReadCase, ...]:
    return (
        ReadCase(
            "experiment-detail",
            f"/experiments/{records.experiment_id}",
            f"/experiments/{records.missing_experiment_id}",
            "Experiment",
            lambda response: response.json()["data"]["experiment_id"],
            records.experiment_id,
        ),
        ReadCase(
            "experiment-sessions",
            f"/experiments/{records.experiment_id}/sessions",
            f"/experiments/{records.missing_experiment_id}/sessions",
            "Experiment",
            lambda response: response.json()["data"][0]["session_id"],
            records.session_id,
        ),
        ReadCase(
            "experiment-datasets",
            f"/experiments/{records.experiment_id}/datasets",
            f"/experiments/{records.missing_experiment_id}/datasets",
            "Experiment",
            lambda response: response.json()["data"][0]["dataset_id"],
            records.dataset_id,
        ),
        ReadCase(
            "session-experiments",
            f"/sessions/{records.session_id}/experiments",
            f"/sessions/{records.missing_session_id}/experiments",
            "Session",
            lambda response: response.json()["data"][0]["experiment_id"],
            records.experiment_id,
        ),
        ReadCase(
            "dataset-experiments",
            f"/datasets/{records.dataset_id}/experiments",
            f"/datasets/{records.missing_dataset_id}/experiments",
            "Dataset",
            lambda response: response.json()["data"][0]["experiment_id"],
            records.experiment_id,
        ),
        ReadCase(
            "session-collections",
            f"/sessions/{records.session_id}/collections",
            f"/sessions/{records.missing_session_id}/collections",
            "Session",
            lambda response: response.json()["data"][0]["collection_id"],
            records.collection_id,
        ),
        ReadCase(
            "collection-snapshots",
            f"/collections/{records.collection_id}/snapshots",
            f"/collections/{records.missing_collection_id}/snapshots",
            "Acquisition collection",
            lambda response: response.json()["data"][0]["snapshot_id"],
            records.snapshot_id,
        ),
        ReadCase(
            "collection-snapshot-detail",
            f"/collection-snapshots/{records.snapshot_id}",
            f"/collection-snapshots/{records.missing_snapshot_id}",
            "Acquisition collection snapshot",
            lambda response: response.json()["data"]["snapshot_id"],
            records.snapshot_id,
        ),
        ReadCase(
            "collection-snapshot-members",
            f"/collection-snapshots/{records.snapshot_id}/members",
            f"/collection-snapshots/{records.missing_snapshot_id}/members",
            "Acquisition collection snapshot",
            lambda response: response.json()["data"][0]["path"],
            "trial/data.bin",
        ),
        ReadCase(
            "collection-snapshot-manifest",
            f"/collection-snapshots/{records.snapshot_id}/manifest",
            f"/collection-snapshots/{records.missing_snapshot_id}/manifest",
            "Acquisition collection snapshot",
            lambda response: response.json()["members"][0]["path"],
            "trial/data.bin",
        ),
    )


def _not_found_body(label: str) -> dict[str, object]:
    return {
        "error": {
            "code": "not_found",
            "message": f"{label} does not exist.",
            "issues": None,
        }
    }


def test_acquisition_reads_are_opaque_and_preserve_authorized_contracts(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
) -> None:
    records = _create_acquisition_records(
        client,
        admin_auth_headers,
        scoped_project_member.hidden_project_id,
    )
    cases = _read_cases(records)
    inventory_ids = {variant.coverage_id for variant in ACQUISITION_READ_OPACITY_VARIANTS}
    assert {f"{ACQUISITION_SUITE}.{case.name}" for case in cases} == inventory_ids

    for case in cases:
        coverage_id = f"{ACQUISITION_SUITE}.{case.name}"
        variant = READ_OPACITY_VARIANTS_BY_ID[coverage_id]
        assert variant.matches_request(
            method="GET",
            request_target=case.existing_path,
            variant="default",
        )
        assert variant.matches_request(
            method="GET",
            request_target=case.missing_path,
            variant="default",
        )

        authorized = client.get(case.existing_path, headers=admin_auth_headers)
        assert authorized.status_code == 200, f"{case.name}: {authorized.text}"
        assert authorized.headers["content-type"].startswith("application/json")
        assert case.authorized_value(authorized) == case.expected_value

        outsider_existing = client.get(
            case.existing_path,
            headers=scoped_project_member.member_headers,
        )
        outsider_missing = client.get(
            case.missing_path,
            headers=scoped_project_member.member_headers,
        )
        assert outsider_existing.status_code == outsider_missing.status_code == 404
        assert (
            outsider_existing.json()
            == outsider_missing.json()
            == _not_found_body(case.not_found_label)
        )


def test_acquisition_reads_still_require_authentication(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
) -> None:
    records = _create_acquisition_records(
        client,
        admin_auth_headers,
        scoped_project_member.hidden_project_id,
    )
    for case in _read_cases(records):
        response = client.get(case.existing_path)
        assert response.status_code == 401, f"{case.name}: {response.text}"
        assert response.json()["error"] == {
            "code": "auth_error",
            "message": "Missing Authorization header.",
            "issues": None,
        }
