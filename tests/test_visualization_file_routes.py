from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _count_suffix(root: Path, suffix: str) -> int:
    if not root.exists():
        return 0
    return sum(1 for _ in root.rglob(f"*{suffix}"))


def _create_visualization_context(
    client: TestClient,
    headers: dict[str, str],
) -> dict[str, str]:
    project_id = client.post(
        "/projects",
        json={"name": "Visualization asset project"},
        headers=headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Can visualizations carry managed files?",
            "question_type": "descriptive",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    dataset_id = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    ).json()["data"]["dataset_id"]
    analysis_id = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "method-1",
            "code_version": "v1",
        },
        headers=headers,
    ).json()["data"]["analysis_id"]
    response = client.post(
        "/visualizations",
        json={
            "analysis_id": analysis_id,
            "viz_type": "figure",
            "file_path": "local/path/figure.png",
            "caption": "Managed figure asset",
        },
        headers=headers,
    )
    assert response.status_code == 201
    return {
        "project_id": project_id,
        "analysis_id": analysis_id,
        "viz_id": response.json()["data"]["viz_id"],
    }


def _create_visualization(client: TestClient, headers: dict[str, str]) -> str:
    return _create_visualization_context(client, headers)["viz_id"]


def _conditional_asset_form(
    content: bytes,
    *,
    expected_current_storage_id: str,
    checksum_sha256: str | None = None,
) -> dict[str, str]:
    return {
        "checksum_sha256": checksum_sha256 or hashlib.sha256(content).hexdigest(),
        "size_bytes": str(len(content)),
        "expected_current_storage_id": expected_current_storage_id,
    }


def test_visualization_file_upload_get_and_download(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    viz_id = _create_visualization(client, headers)
    storage_root = Path(client.app.state.file_storage_backend.base_path)
    content = b"\x89PNG\r\n\x1a\nfigure-bytes"
    checksum = hashlib.sha256(content).hexdigest()

    upload = client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("figure.png", content, "image/png")},
        headers=headers,
    )

    assert upload.status_code == 201
    payload = upload.json()["data"]
    assert payload["file_path"] == "local/path/figure.png"
    assert payload["asset"]["filename"] == "figure.png"
    assert payload["asset"]["content_type"] == "image/png"
    assert payload["asset"]["size_bytes"] == len(content)
    assert payload["asset"]["checksum"] == checksum
    assert payload["asset_download_path"] == f"/visualizations/{viz_id}/file/download"
    assert _count_suffix(storage_root, ".bin") == 1
    assert _count_suffix(storage_root, ".json") == 1

    fetched = client.get(f"/visualizations/{viz_id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["data"]["asset"]["checksum"] == checksum

    download = client.get(f"/visualizations/{viz_id}/file/download", headers=headers)
    assert download.status_code == 200
    assert download.content == content
    assert download.headers["content-disposition"] == 'attachment; filename="figure.png"'
    assert download.headers["content-length"] == str(len(content))
    assert download.headers["content-type"].startswith("image/png")
    assert download.headers["x-content-type-options"] == "nosniff"


def test_visualization_file_upload_rejects_browser_executable_content_type(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    viz_id = _create_visualization(client, headers)

    upload = client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("attack.html", b"<script>alert(1)</script>", "text/html")},
        headers=headers,
    )

    assert upload.status_code == 422
    assert "not allowed" in upload.json()["error"]["message"]


def test_visualization_file_upload_replaces_old_asset(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    viz_id = _create_visualization(client, headers)
    storage_root = Path(client.app.state.file_storage_backend.base_path)

    first = client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("first.png", b"first-bytes", "image/png")},
        headers=headers,
    )
    assert first.status_code == 201
    second_content = b"second-bytes"
    second = client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("second.png", second_content, "image/png")},
        headers=headers,
    )

    assert second.status_code == 201
    payload = second.json()["data"]
    assert payload["asset"]["filename"] == "second.png"
    assert payload["asset"]["checksum"] == hashlib.sha256(second_content).hexdigest()
    assert _count_suffix(storage_root, ".bin") == 1
    assert _count_suffix(storage_root, ".json") == 1


def test_visualization_file_conditional_retry_reuses_identical_asset(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    viz_id = _create_visualization(client, admin_auth_headers)
    storage_root = Path(client.app.state.file_storage_backend.base_path)
    content = b"retry-safe-figure"
    form = _conditional_asset_form(
        content,
        expected_current_storage_id="absent",
    )

    first = client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("figure.png", content, "image/png")},
        data=form,
        headers=admin_auth_headers,
    )
    second = client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("figure.png", content, "image/png")},
        data=form,
        headers=admin_auth_headers,
    )

    assert first.status_code == 201
    assert first.json()["meta"] == {"asset_outcome": "created"}
    assert second.status_code == 200
    assert second.json()["meta"] == {"asset_outcome": "reused"}
    assert first.json()["data"]["asset"] == second.json()["data"]["asset"]
    assert _count_suffix(storage_root, ".bin") == 1
    assert _count_suffix(storage_root, ".json") == 1


def test_visualization_file_stale_precondition_preserves_intervening_asset(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    viz_id = _create_visualization(client, admin_auth_headers)
    storage_root = Path(client.app.state.file_storage_backend.base_path)
    original = b"current-figure"
    stale = b"stale-retry-figure"
    first = client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("current.png", original, "image/png")},
        headers=admin_auth_headers,
    )
    assert first.status_code == 201

    conflict = client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("stale.png", stale, "image/png")},
        data=_conditional_asset_form(
            stale,
            expected_current_storage_id="absent",
        ),
        headers=admin_auth_headers,
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"
    fetched = client.get(f"/visualizations/{viz_id}", headers=admin_auth_headers)
    assert fetched.json()["data"]["asset"] == first.json()["data"]["asset"]
    download = client.get(
        f"/visualizations/{viz_id}/file/download",
        headers=admin_auth_headers,
    )
    assert download.content == original
    assert _count_suffix(storage_root, ".bin") == 1
    assert _count_suffix(storage_root, ".json") == 1


def test_visualization_file_conditional_replacement_uses_observed_storage_id(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    viz_id = _create_visualization(client, admin_auth_headers)
    storage_root = Path(client.app.state.file_storage_backend.base_path)
    first = client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("first.png", b"first-figure", "image/png")},
        headers=admin_auth_headers,
    )
    assert first.status_code == 201
    previous_storage_id = first.json()["data"]["asset"]["storage_id"]
    replacement = b"replacement-figure"

    second = client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("second.png", replacement, "image/png")},
        data=_conditional_asset_form(
            replacement,
            expected_current_storage_id=previous_storage_id,
        ),
        headers=admin_auth_headers,
    )

    assert second.status_code == 201
    assert second.json()["meta"] == {"asset_outcome": "replaced"}
    assert second.json()["data"]["asset"]["storage_id"] != previous_storage_id
    assert second.json()["data"]["asset"]["checksum"] == hashlib.sha256(replacement).hexdigest()
    assert _count_suffix(storage_root, ".bin") == 1
    assert _count_suffix(storage_root, ".json") == 1


def test_visualization_file_integrity_mismatch_cleans_candidate_and_keeps_current(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    viz_id = _create_visualization(client, admin_auth_headers)
    storage_root = Path(client.app.state.file_storage_backend.base_path)
    original = b"verified-current-figure"
    replacement = b"corrupted-replacement"
    first = client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("current.png", original, "image/png")},
        headers=admin_auth_headers,
    )
    assert first.status_code == 201
    current_storage_id = first.json()["data"]["asset"]["storage_id"]

    mismatch = client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("replacement.png", replacement, "image/png")},
        data=_conditional_asset_form(
            replacement,
            expected_current_storage_id=current_storage_id,
            checksum_sha256="0" * 64,
        ),
        headers=admin_auth_headers,
    )

    assert mismatch.status_code == 422
    assert "checksum_sha256" in mismatch.json()["error"]["message"]
    fetched = client.get(f"/visualizations/{viz_id}", headers=admin_auth_headers)
    assert fetched.json()["data"]["asset"] == first.json()["data"]["asset"]
    assert _count_suffix(storage_root, ".bin") == 1
    assert _count_suffix(storage_root, ".json") == 1


def test_visualization_file_download_requires_existing_asset(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    viz_id = _create_visualization(client, admin_auth_headers)

    response = client.get(
        f"/visualizations/{viz_id}/file/download",
        headers=admin_auth_headers,
    )

    assert response.status_code == 404


def test_visualization_file_download_requires_project_access(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    viz_id = _create_visualization(client, headers)
    upload = client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("access.png", b"access-bytes", "image/png")},
        headers=headers,
    )
    assert upload.status_code == 201
    outsider = client.post(
        "/auth/register",
        json={"username": f"viz-outsider-{viz_id[:8]}", "password": "secret"},
    )
    assert outsider.status_code == 201

    response = client.get(
        f"/visualizations/{viz_id}/file/download",
        headers=_auth_headers(outsider.json()["data"]["access_token"]),
    )

    assert response.status_code == 401


def test_visualization_delete_removes_managed_file_from_storage(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    viz_id = _create_visualization(client, headers)
    storage_root = Path(client.app.state.file_storage_backend.base_path)
    upload = client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("cleanup.png", b"cleanup-bytes", "image/png")},
        headers=headers,
    )
    assert upload.status_code == 201
    assert _count_suffix(storage_root, ".bin") == 1

    delete = client.delete(f"/visualizations/{viz_id}", headers=headers)

    assert delete.status_code == 200
    assert _count_suffix(storage_root, ".bin") == 0
    assert _count_suffix(storage_root, ".json") == 0


def test_analysis_delete_removes_visualization_managed_files_from_storage(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    context = _create_visualization_context(client, admin_auth_headers)
    storage_root = Path(client.app.state.file_storage_backend.base_path)
    upload = client.post(
        f"/visualizations/{context['viz_id']}/file",
        files={"file": ("analysis-cleanup.png", b"cleanup-bytes", "image/png")},
        headers=admin_auth_headers,
    )
    assert upload.status_code == 201
    assert _count_suffix(storage_root, ".bin") == 1

    delete = client.delete(
        f"/analyses/{context['analysis_id']}",
        headers=admin_auth_headers,
    )

    assert delete.status_code == 200
    assert _count_suffix(storage_root, ".bin") == 0
    assert _count_suffix(storage_root, ".json") == 0


def test_project_delete_removes_visualization_managed_files_from_storage(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    context = _create_visualization_context(client, headers)
    storage_root = Path(client.app.state.file_storage_backend.base_path)
    upload = client.post(
        f"/visualizations/{context['viz_id']}/file",
        files={"file": ("project-cleanup.png", b"cleanup-bytes", "image/png")},
        headers=headers,
    )
    assert upload.status_code == 201
    assert _count_suffix(storage_root, ".bin") == 1

    delete = client.delete(f"/projects/{context['project_id']}", headers=headers)

    assert delete.status_code == 200
    assert _count_suffix(storage_root, ".bin") == 0
    assert _count_suffix(storage_root, ".json") == 0
