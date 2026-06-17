from __future__ import annotations

import json

import httpx
import pytest

from lab_tracker_client import (
    EntityRef,
    LabTracker,
    LTValidationError,
    build_evidence_metadata,
    file_sha256,
    first_line_marker,
)


def _json_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def test_upsert_project_question_and_note_are_idempotent() -> None:
    projects: list[dict] = []
    questions: list[dict] = []
    notes: list[dict] = []
    post_counts = {"/projects": 0, "/questions": 0, "/notes": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/projects":
            return _json_response(
                200,
                {"data": projects, "meta": {"limit": 200, "offset": 0, "total": len(projects)}},
            )
        if request.method == "POST" and request.url.path == "/projects":
            post_counts["/projects"] += 1
            body = json.loads(request.content.decode("utf-8"))
            project = {
                "project_id": "project-1",
                "name": body["name"],
                "description": body["description"],
                "status": body.get("status") or "active",
            }
            projects.append(project)
            return _json_response(201, {"data": project})
        if request.method == "GET" and request.url.path == "/questions":
            assert request.url.params["project_id"] == "project-1"
            return _json_response(
                200,
                {
                    "data": questions,
                    "meta": {"limit": 200, "offset": 0, "total": len(questions)},
                },
            )
        if request.method == "POST" and request.url.path == "/questions":
            post_counts["/questions"] += 1
            body = json.loads(request.content.decode("utf-8"))
            question = {"question_id": "question-1", **body}
            questions.append(question)
            return _json_response(201, {"data": question})
        if request.method == "GET" and request.url.path == "/notes":
            assert request.url.params["project_id"] == "project-1"
            return _json_response(
                200,
                {"data": notes, "meta": {"limit": 200, "offset": 0, "total": len(notes)}},
            )
        if request.method == "POST" and request.url.path == "/notes":
            post_counts["/notes"] += 1
            body = json.loads(request.content.decode("utf-8"))
            note = {"note_id": "note-1", **body}
            notes.append(note)
            return _json_response(201, {"data": note})
        return _json_response(404, {"error": {"message": "not found"}})

    with LabTracker(
        base_url="http://testserver",
        transport=httpx.MockTransport(handler),
    ) as lt:
        project = lt.upsert_project(name="Literature geometry", description="consumer sync")
        same_project = lt.upsert_project(
            name="Literature geometry",
            description="ignored after first create",
        )
        question = lt.upsert_question(
            project_id=project.id,
            text="How does the manifold organize claims?",
            question_type="descriptive",
            status="active",
        )
        same_question = lt.upsert_question(
            project_id=project.project_id,
            text="How does the manifold organize claims?",
            question_type="descriptive",
        )
        note = lt.upsert_note(
            project_id=project.id,
            content="\n# Fig. 1 marker\n\nBody text",
            targets=[EntityRef("question", question.id)],
            metadata={"category": "findings"},
        )
        same_note = lt.upsert_note(
            project_id=project.id,
            content="# Fig. 1 marker\n\nUpdated body that should not duplicate",
            targets=[("question", question.id)],
        )

    assert project.id == "project-1"
    assert same_project is not project
    assert same_project.id == project.id
    assert same_question.question_id == question.question_id
    assert note.id == same_note.id == "note-1"
    assert notes[0]["targets"] == [{"entity_type": "question", "entity_id": "question-1"}]
    assert post_counts == {"/projects": 1, "/questions": 1, "/notes": 1}


def test_bad_enum_values_fail_before_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _json_response(200, {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}})

    lt = LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler))

    with pytest.raises(LTValidationError, match="question_type"):
        lt.upsert_question(
            project_id="project-1",
            text="Bad enum",
            question_type="broad",
        )
    with pytest.raises(LTValidationError, match="note status"):
        lt.upsert_note(
            project_id="project-1",
            content="# marker",
            status="active",
        )

    lt.close()
    assert requests == []


def test_entity_ref_and_first_line_marker_helpers() -> None:
    assert first_line_marker("\n\n  # Marker  \nbody") == "# Marker"
    assert EntityRef("question", "question-1").to_payload() == {
        "entity_type": "question",
        "entity_id": "question-1",
    }
    with pytest.raises(LTValidationError, match="entity_type"):
        EntityRef("bad-kind", "id")


def test_quick_capture_posts_text_as_multipart_file() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "POST"
        assert request.url.path == "/notes/quick-capture"
        assert b"project-1" in request.content
        assert b"agent observation" in request.content
        assert b"quick.txt" in request.content
        assert b'"source": "cli"' in request.content
        return _json_response(
            202,
            {
                "data": {
                    "note_id": "note-quick",
                    "project_id": "project-1",
                    "status": "staged",
                }
            },
        )

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-1",
        transport=httpx.MockTransport(handler),
    ) as lt:
        note = lt.quick_capture(
            "agent observation",
            filename="quick.txt",
            source="cli",
        )

    assert note.id == "note-quick"
    assert len(seen) == 1


def test_upload_note_file_posts_staged_evidence_multipart(tmp_path) -> None:
    evidence_path = tmp_path / "bench.md"
    evidence_path.write_text("bench observation", encoding="utf-8")
    metadata = build_evidence_metadata(
        source_provider="local-folder",
        source_uri=evidence_path.resolve().as_uri(),
        source_external_id="bench.md",
        content_hash=file_sha256(evidence_path),
        capture_kind="file",
        adapter="test-adapter",
        title="bench.md",
        observed_at="2026-06-11T12:00:00+00:00",
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = request.content
        assert request.method == "POST"
        assert request.url.path == "/notes/upload-file"
        assert b"bench observation" in body
        assert b"bench.md" in body
        assert b"staged" in body
        assert b"evidence_source_provider" in body
        assert b"local-folder" in body
        assert b"evidence_content_hash" in body
        return _json_response(
            201,
            {
                "data": {
                    "note_id": "note-file",
                    "project_id": "project-1",
                    "status": "staged",
                    "metadata": metadata,
                }
            },
        )

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        note = lt.upload_note_file(
            project_id="project-1",
            file_path=evidence_path,
            metadata=metadata,
        )

    assert note.id == "note-file"
    assert len(seen) == 1


def test_import_evidence_file_skips_duplicate_and_imports_changed_content(tmp_path) -> None:
    evidence_path = tmp_path / "bench.md"
    evidence_path.write_text("bench observation", encoding="utf-8")
    original_hash = file_sha256(evidence_path)
    notes = [
        {
            "note_id": "note-existing",
            "project_id": "project-1",
            "metadata": {
                "evidence_source_provider": "local-folder",
                "evidence_source_external_id": "bench.md",
                "evidence_content_hash": original_hash,
            },
        }
    ]
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200,
                {"data": notes, "meta": {"limit": 200, "offset": 0, "total": len(notes)}},
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            post_count += 1
            return _json_response(
                201,
                {
                    "data": {
                        "note_id": "note-new",
                        "project_id": "project-1",
                        "metadata": {},
                    }
                },
            )
        return _json_response(404, {"error": {"message": "not found"}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        duplicate = lt.import_evidence_file(
            project_id="project-1",
            file_path=evidence_path,
            source_external_id="bench.md",
        )
        evidence_path.write_text("changed observation", encoding="utf-8")
        changed = lt.import_evidence_file(
            project_id="project-1",
            file_path=evidence_path,
            source_external_id="bench.md",
        )

    assert duplicate.action == "skipped"
    assert duplicate.reason == "duplicate"
    assert duplicate.note and duplicate.note.id == "note-existing"
    assert changed.action == "imported"
    assert changed.note and changed.note.id == "note-new"
    assert post_count == 1


def test_import_evidence_file_hashes_uploaded_bytes_from_one_read(tmp_path) -> None:
    evidence_path = tmp_path / "bench.md"
    evidence_path.write_text("stable observation", encoding="utf-8")
    stable_hash = file_sha256(evidence_path)
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "GET" and request.url.path == "/notes":
            evidence_path.write_text("changed observation", encoding="utf-8")
            return _json_response(
                200,
                {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}},
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            post_count += 1
            body = request.content
            assert b"stable observation" in body
            assert b"changed observation" not in body
            assert stable_hash.encode("utf-8") in body
            return _json_response(
                201,
                {"data": {"note_id": "note-imported", "project_id": "project-1"}},
            )
        return _json_response(404, {"error": {"message": "not found"}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        result = lt.import_evidence_file(
            project_id="project-1",
            file_path=evidence_path,
            source_external_id="bench.md",
        )

    assert result.action == "imported"
    assert result.content_hash == stable_hash
    assert post_count == 1


def test_build_evidence_metadata_preserves_external_id_whitespace() -> None:
    metadata = build_evidence_metadata(
        source_provider="local-folder",
        source_uri="file:///tmp/root/%20leading.md",
        source_external_id="  leading.md",
        content_hash="abc123",
        capture_kind="file",
        adapter="test-adapter",
        title="leading.md",
    )

    assert metadata["evidence_source_external_id"] == "  leading.md"


def test_auth_login_and_retry_after_unauthorized() -> None:
    seen: list[tuple[str, str, str | None]] = []
    projects_response_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal projects_response_count
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/auth/login":
            return _json_response(200, {"data": {"access_token": "token-1"}})
        if request.url.path == "/projects":
            projects_response_count += 1
            if projects_response_count == 1:
                return _json_response(401, {"error": {"message": "expired"}})
            return _json_response(
                200,
                {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}},
            )
        return _json_response(404, {"error": {"message": "not found"}})

    with LabTracker(
        base_url="http://testserver",
        username="service",
        password="secret",
        transport=httpx.MockTransport(handler),
    ) as lt:
        assert lt.list_projects() == []

    assert seen == [
        ("POST", "/auth/login", None),
        ("GET", "/projects", "Bearer token-1"),
        ("POST", "/auth/login", None),
        ("GET", "/projects", "Bearer token-1"),
    ]
