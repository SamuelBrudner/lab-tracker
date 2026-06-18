from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from lab_tracker.errors import PayloadTooLargeError, ValidationError
from lab_tracker.upload_security import (
    enforce_request_content_length_limit,
    validate_upload_content_type,
)


def test_upload_content_type_validation_blocks_browser_executable_types():
    with pytest.raises(ValidationError, match="not allowed"):
        validate_upload_content_type("text/html; charset=utf-8")

    with pytest.raises(ValidationError, match="not allowed"):
        validate_upload_content_type("image/svg+xml")

    assert validate_upload_content_type("image/png") == "image/png"
    assert validate_upload_content_type(None) == "application/octet-stream"


def test_request_content_length_limit_rejects_oversized_payload():
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [(b"content-length", b"100")],
        }
    )

    with pytest.raises(PayloadTooLargeError, match="configured limit"):
        enforce_request_content_length_limit(request, max_bytes=10)


def test_note_file_upload_rejects_browser_executable_content_type(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = client.post(
        "/projects",
        json={"name": "Upload content type project"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]

    upload = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("attack.html", b"<script>alert(1)</script>", "text/html")},
        headers=admin_auth_headers,
    )

    assert upload.status_code == 422
    assert "not allowed" in upload.json()["error"]["message"]
