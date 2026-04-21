from __future__ import annotations

from fastapi.testclient import TestClient


def test_dataset_review_request_queue_approve_commits_dataset(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers

    me_response = client.get("/auth/me", headers=headers)
    assert me_response.status_code == 200
    reviewer_user_id = me_response.json()["data"]["user_id"]

    project_response = client.post(
        "/projects",
        json={"name": "Review flow", "review_policy": "all"},
        headers=headers,
    )
    assert project_response.status_code == 201
    project_id = project_response.json()["data"]["project_id"]

    question_response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Is this dataset ready to commit?",
            "question_type": "descriptive",
        },
        headers=headers,
    )
    assert question_response.status_code == 201
    question_id = question_response.json()["data"]["question_id"]

    activate_response = client.patch(
        f"/questions/{question_id}",
        json={"status": "active"},
        headers=headers,
    )
    assert activate_response.status_code == 200

    dataset_response = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    )
    assert dataset_response.status_code == 201
    dataset_id = dataset_response.json()["data"]["dataset_id"]

    attach_response = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("data.bin", b"real-data", "application/octet-stream")},
        headers=headers,
    )
    assert attach_response.status_code == 201

    request_review_response = client.post(
        f"/datasets/{dataset_id}/review",
        json={"comments": "please review"},
        headers=headers,
    )
    assert request_review_response.status_code == 201
    review_payload = request_review_response.json()["data"]
    assert review_payload["dataset_id"] == dataset_id
    assert review_payload["status"] == "pending"
    assert review_payload["reviewer_user_id"] == reviewer_user_id
    assert review_payload["comments"] == "please review"

    get_review_response = client.get(f"/datasets/{dataset_id}/review", headers=headers)
    assert get_review_response.status_code == 200
    assert get_review_response.json()["data"]["review_id"] == review_payload["review_id"]

    queue_response = client.get("/reviews/pending", headers=headers)
    assert queue_response.status_code == 200
    queue_payload = queue_response.json()
    assert queue_payload["meta"]["total"] == 1
    assert queue_payload["data"][0]["review_id"] == review_payload["review_id"]

    approve_response = client.patch(
        f"/datasets/{dataset_id}/review",
        json={"action": "approve", "comments": "looks good"},
        headers=headers,
    )
    assert approve_response.status_code == 200
    approved_payload = approve_response.json()["data"]
    assert approved_payload["status"] == "approved"
    assert approved_payload["comments"] == "looks good"
    assert approved_payload["resolved_at"] is not None

    dataset_after = client.get(f"/datasets/{dataset_id}", headers=headers)
    assert dataset_after.status_code == 200
    assert dataset_after.json()["data"]["status"] == "committed"

    queue_after = client.get("/reviews/pending", headers=headers)
    assert queue_after.status_code == 200
    assert queue_after.json()["meta"]["total"] == 0


def test_dataset_review_reject_leaves_dataset_staged_and_persists_comments(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers

    project_id = client.post(
        "/projects",
        json={"name": "Review reject", "review_policy": "all"},
        headers=headers,
    ).json()["data"]["project_id"]

    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does rejection keep datasets staged?",
            "question_type": "descriptive",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    activate_response = client.patch(
        f"/questions/{question_id}",
        json={"status": "active"},
        headers=headers,
    )
    assert activate_response.status_code == 200

    dataset_id = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    ).json()["data"]["dataset_id"]

    attach_response = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("data.bin", b"real-data", "application/octet-stream")},
        headers=headers,
    )
    assert attach_response.status_code == 201

    request_review = client.post(
        f"/datasets/{dataset_id}/review",
        json={"comments": "requesting review"},
        headers=headers,
    )
    assert request_review.status_code == 201

    reject_response = client.patch(
        f"/datasets/{dataset_id}/review",
        json={"action": "reject", "comments": "not acceptable"},
        headers=headers,
    )
    assert reject_response.status_code == 200
    rejected_payload = reject_response.json()["data"]
    assert rejected_payload["status"] == "rejected"
    assert rejected_payload["comments"] == "not acceptable"

    dataset_after = client.get(f"/datasets/{dataset_id}", headers=headers)
    assert dataset_after.status_code == 200
    assert dataset_after.json()["data"]["status"] == "staged"

    review_after = client.get(f"/datasets/{dataset_id}/review", headers=headers)
    assert review_after.status_code == 200
    assert review_after.json()["data"]["status"] == "rejected"
    assert review_after.json()["data"]["comments"] == "not acceptable"


def test_project_patch_can_set_review_policy(
    client: TestClient, admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers

    resp = client.post(
        "/projects", json={"name": "Review policy patch"}, headers=headers,
    )
    project_id = resp.json()["data"]["project_id"]

    update_response = client.patch(
        f"/projects/{project_id}",
        json={"review_policy": "all"},
        headers=headers,
    )
    assert update_response.status_code == 200
    assert update_response.json()["data"]["review_policy"] == "all"


def test_dataset_commit_with_none_policy_skips_review_queue(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers

    project_id = client.post(
        "/projects",
        json={"name": "No review policy", "review_policy": "none"},
        headers=headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Should commit skip review?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    dataset_id = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    ).json()["data"]["dataset_id"]

    attach_response = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("data.bin", b"real-data", "application/octet-stream")},
        headers=headers,
    )
    assert attach_response.status_code == 201

    commit_response = client.patch(
        f"/datasets/{dataset_id}",
        json={"status": "committed"},
        headers=headers,
    )
    assert commit_response.status_code == 200
    assert commit_response.json()["data"]["status"] == "committed"

    review_response = client.get(f"/datasets/{dataset_id}/review", headers=headers)
    assert review_response.status_code == 404

    queue_response = client.get("/reviews/pending", headers=headers)
    assert queue_response.status_code == 200
    assert queue_response.json()["meta"]["total"] == 0


def test_project_rejects_selective_review_policy(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    response = client.post(
        "/projects",
        json={"name": "Selective policy", "review_policy": "selective"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"
