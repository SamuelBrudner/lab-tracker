from __future__ import annotations

import importlib
import json

import httpx
import pytest

from lab_tracker_client import (
    EntityRef,
    LabTracker,
    LTAPIError,
    LTConflictError,
    LTRecord,
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
        # Re-issuing without a description reuses the existing project (a bare
        # upsert stays lenient); a *conflicting* description would now raise.
        same_project = lt.upsert_project(name="Literature geometry")
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
            content="# Fig. 1 marker\n\nBody text",
            targets=[("question", question.id)],
        )

    assert project.id == "project-1"
    assert same_project is not project
    assert same_project.id == project.id
    assert same_question.question_id == question.question_id
    assert note.id == same_note.id == "note-1"
    assert notes[0]["targets"] == [{"entity_type": "question", "entity_id": "question-1"}]
    assert post_counts == {"/projects": 1, "/questions": 1, "/notes": 1}


def test_upsert_note_raises_when_marker_matches_different_content() -> None:
    notes = [{"note_id": "note-1", "raw_content": "# marker\n\nOriginal body"}]
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "GET" and request.url.path == "/notes":
            return _json_response(
                200,
                {"data": notes, "meta": {"limit": 200, "offset": 0, "total": len(notes)}},
            )
        if request.method == "POST" and request.url.path == "/notes":
            post_count += 1
            return _json_response(201, {"data": {"note_id": "duplicate-note"}})
        return _json_response(404, {"error": {"message": "not found"}})

    with (
        LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt,
        pytest.raises(LTValidationError, match="same first-line marker"),
    ):
        lt.upsert_note(project_id="project-1", content="# marker\n\nNew body")

    assert post_count == 0


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


def test_upsert_question_and_note_omit_status_to_inherit_server_staged_default() -> None:
    """Consumers must not cross the human gate implicitly: omitting status must

    drop the key from the POST body so the server applies its canonical staged
    default, rather than the client re-declaring active/committed.
    """

    bodies: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path in {"/questions", "/notes"}:
            return _json_response(
                200, {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        if request.method == "POST" and path == "/questions":
            bodies["question"] = json.loads(request.content.decode("utf-8"))
            return _json_response(201, {"data": {"question_id": "question-1"}})
        if request.method == "POST" and path == "/notes":
            bodies["note"] = json.loads(request.content.decode("utf-8"))
            return _json_response(201, {"data": {"note_id": "note-1"}})
        return _json_response(404, {"error": {"message": "not found"}})

    with LabTracker(
        base_url="http://testserver", transport=httpx.MockTransport(handler)
    ) as lt:
        lt.upsert_question(project_id="project-1", text="How does it stage?")
        lt.upsert_note(project_id="project-1", content="# marker\n\nBody")

    assert "status" not in bodies["question"]
    assert "status" not in bodies["note"]


def test_activate_question_and_commit_note_are_explicit_patches() -> None:
    """Crossing the human gate is a named, explicit PATCH, not a create default."""

    patched: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            patched.append((request.url.path, json.loads(request.content.decode("utf-8"))))
            key = "question_id" if "questions" in request.url.path else "note_id"
            return _json_response(200, {"data": {key: "id-1", "status": "changed"}})
        return _json_response(404, {"error": {"message": "not found"}})

    with LabTracker(
        base_url="http://testserver", transport=httpx.MockTransport(handler)
    ) as lt:
        lt.activate_question("question-1")
        lt.commit_note("note-1")

    assert patched == [
        ("/questions/question-1", {"status": "active"}),
        ("/notes/note-1", {"status": "committed"}),
    ]


def test_get_or_create_project_distinguishes_created_reused_conflict_and_update() -> None:
    projects: list[dict] = []
    post_bodies: list[dict] = []
    patched: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/projects":
            return _json_response(
                200,
                {"data": projects, "meta": {"limit": 200, "offset": 0, "total": len(projects)}},
            )
        if request.method == "POST" and path == "/projects":
            body = json.loads(request.content.decode("utf-8"))
            post_bodies.append(body)
            project = {
                "project_id": "project-1",
                "name": body["name"],
                "description": body.get("description", ""),
                "status": body.get("status") or "active",
            }
            projects.append(project)
            return _json_response(201, {"data": project})
        if request.method == "PATCH" and path == "/projects/project-1":
            body = json.loads(request.content.decode("utf-8"))
            patched.append((path, body))
            projects[0].update(body)
            return _json_response(200, {"data": projects[0]})
        return _json_response(404, {"error": {"message": "not found"}})

    with LabTracker(
        base_url="http://testserver", transport=httpx.MockTransport(handler)
    ) as lt:
        created = lt.get_or_create_project(name="Geometry", description="first")
        assert created.action == "created"
        assert created.id == "project-1"
        # A deterministic idempotency key is sent so concurrent creates dedupe.
        assert post_bodies[0]["client_capture_id"].startswith("gc:")

        reused = lt.get_or_create_project(name="Geometry")
        assert reused.action == "reused"
        assert reused.id == "project-1"

        # A conflicting supplied field is never silently discarded.
        with pytest.raises(LTValidationError, match="differs"):
            lt.get_or_create_project(name="Geometry", description="conflicting")

        updated = lt.get_or_create_project(
            name="Geometry", description="conflicting", on_conflict="update"
        )
        assert updated.action == "updated"
        assert patched == [("/projects/project-1", {"description": "conflicting"})]

    assert len(post_bodies) == 1  # one POST despite four get_or_create calls


def test_get_or_create_question_distinguishes_created_reused_and_conflict() -> None:
    questions: list[dict] = []
    post_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/questions":
            return _json_response(
                200,
                {"data": questions, "meta": {"limit": 200, "offset": 0, "total": len(questions)}},
            )
        if request.method == "POST" and path == "/questions":
            body = json.loads(request.content.decode("utf-8"))
            post_bodies.append(body)
            question = {"question_id": "question-1", **body}
            questions.append(question)
            return _json_response(201, {"data": question})
        return _json_response(404, {"error": {"message": "not found"}})

    with LabTracker(
        base_url="http://testserver", transport=httpx.MockTransport(handler)
    ) as lt:
        created = lt.get_or_create_question(
            project_id="project-1", text="How?", question_type="descriptive"
        )
        assert created.action == "created"
        assert post_bodies[0]["client_capture_id"].startswith("gc:")

        reused = lt.get_or_create_question(project_id="project-1", text="How?")
        assert reused.action == "reused"

        with pytest.raises(LTValidationError, match="differs"):
            lt.get_or_create_question(
                project_id="project-1", text="How?", question_type="hypothesis_driven"
            )

    assert len(post_bodies) == 1


@pytest.mark.parametrize(
    ("method_name", "list_path", "post_payload", "record"),
    [
        (
            "get_or_create_project",
            "/projects",
            {"name": "Concurrent project"},
            {"project_id": "project-winner", "name": "Concurrent project"},
        ),
        (
            "get_or_create_question",
            "/questions",
            {"project_id": "project-1", "text": "Concurrent question?"},
            {
                "question_id": "question-winner",
                "project_id": "project-1",
                "text": "Concurrent question?",
            },
        ),
    ],
)
def test_get_or_create_reports_server_side_race_reuse(
    method_name: str,
    list_path: str,
    post_payload: dict[str, str],
    record: dict[str, str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == list_path:
            return _json_response(
                200,
                {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}},
            )
        if request.method == "POST" and request.url.path == list_path:
            return _json_response(200, {"data": record})
        return _json_response(404, {"error": {"message": "not found"}})

    with LabTracker(
        base_url="http://testserver", transport=httpx.MockTransport(handler)
    ) as lt:
        result = getattr(lt, method_name)(**post_payload)

    assert result.action == "reused"
    assert result.record == record


def test_http_409_maps_to_conflict_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/projects":
            return _json_response(
                200,
                {"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}},
            )
        return _json_response(
            409,
            {"error": {"code": "conflict", "message": "capture key conflict"}},
        )

    with (
        LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt,
        pytest.raises(LTConflictError, match="capture key conflict"),
    ):
        lt.get_or_create_project(name="Conflict")


def test_upload_409_maps_to_conflict_error(tmp_path) -> None:
    upload = tmp_path / "conflict.txt"
    upload.write_text("different capture content", encoding="utf-8")

    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            409,
            {"error": {"code": "conflict", "message": "upload key conflict"}},
        )

    with (
        LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt,
        pytest.raises(LTConflictError, match="upload key conflict"),
    ):
        lt.upload_note_file(
            project_id="project-1",
            file_path=upload,
            client_capture_id="duplicate-upload-key",
        )


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
        assert b"client-capture-quick" in request.content
        return _json_response(
            201,
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
            client_capture_id="client-capture-quick",
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
        assert b"client-capture-file" in body
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
            client_capture_id="client-capture-file",
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


def test_visualization_record_id_prefers_visualization_id() -> None:
    record = LTRecord({"analysis_id": "analysis-1", "viz_id": "viz-1"})

    assert record.id == "viz-1"


def test_create_analysis_graph_draft_posts_note_route() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "POST"
        assert request.url.path == "/notes/note-1/analysis-graph-drafts"
        return _json_response(
            201,
            {"data": {"change_set_id": "draft-1", "project_id": "project-1"}},
        )

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        draft = lt.create_analysis_graph_draft("note-1")

    assert draft.id == "draft-1"
    assert len(seen) == 1


def test_list_limit_is_total_cap_not_page_size() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.params["limit"], request.url.params["offset"]))
        return _json_response(
            200,
            {
                "data": [
                    {"project_id": "project-1", "name": "One"},
                    {"project_id": "project-2", "name": "Two"},
                    {"project_id": "project-3", "name": "Three"},
                ],
                "meta": {"limit": 3, "offset": 0, "total": 50},
            },
        )

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        projects = lt.list_projects(limit=3)

    assert [project.id for project in projects] == ["project-1", "project-2", "project-3"]
    assert requests == [("3", "0")]


def test_upsert_note_finds_existing_marker_after_first_page() -> None:
    notes = [
        {
            "note_id": f"note-{index}",
            "project_id": "project-1",
            "raw_content": f"# marker {index}\n\nBody",
        }
        for index in range(250)
    ]
    notes[240]["raw_content"] = "# target marker\n\nExisting body"
    requests: list[tuple[str, str]] = []
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "GET" and request.url.path == "/notes":
            assert request.url.params["project_id"] == "project-1"
            limit = int(request.url.params["limit"])
            offset = int(request.url.params["offset"])
            requests.append((str(limit), str(offset)))
            return _json_response(
                200,
                {
                    "data": notes[offset : offset + limit],
                    "meta": {"limit": limit, "offset": offset, "total": len(notes)},
                },
            )
        if request.method == "POST" and request.url.path == "/notes":
            post_count += 1
            return _json_response(201, {"data": {"note_id": "duplicate-note"}})
        return _json_response(404, {"error": {"message": "not found"}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        note = lt.upsert_note(
            project_id="project-1",
            content="# target marker\n\nExisting body",
        )

    assert note.id == "note-240"
    assert requests == [("200", "0"), ("200", "200")]
    assert post_count == 0


def test_transport_errors_use_client_error_hierarchy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with (
        LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt,
        pytest.raises(LTAPIError, match="GET /projects failed: offline"),
    ):
        lt.list_projects()


def test_rejected_supplied_access_token_does_not_report_missing_credentials() -> None:
    requests: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, request.headers.get("authorization")))
        return _json_response(401, {"error": {"message": "expired"}})

    with (
        LabTracker(
            base_url="http://testserver",
            access_token="stale-token",
            transport=httpx.MockTransport(handler),
        ) as lt,
        pytest.raises(LTAPIError, match="LAB_TRACKER_ACCESS_TOKEN was rejected"),
    ):
        lt.list_projects()

    assert requests == [("/projects", "Bearer stale-token")]


def test_readiness_uses_credentials_when_configured() -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/auth/login":
            return _json_response(200, {"data": {"access_token": "token-1"}})
        if request.url.path == "/readiness":
            return _json_response(200, {"status": "ok"})
        return _json_response(404, {"error": {"message": "not found"}})

    with LabTracker(
        base_url="http://testserver",
        username="service",
        password="secret",
        transport=httpx.MockTransport(handler),
    ) as lt:
        assert lt.readiness() == {"status": "ok"}

    assert seen == [
        ("POST", "/auth/login", None),
        ("GET", "/readiness", "Bearer token-1"),
    ]


def test_from_env_uses_mcp_base_url_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAB_TRACKER_BASE_URL", raising=False)
    monkeypatch.setenv("LAB_TRACKER_MCP_BASE_URL", "http://lab.example.test:8123/")
    monkeypatch.setenv("LAB_TRACKER_HTTP_TIMEOUT", "15")

    lt = LabTracker.from_env()
    try:
        assert lt.base_url == "http://lab.example.test:8123"
    finally:
        lt.close()


def test_lazy_client_context_manager_does_not_close_shared_singleton() -> None:
    """`with client as lt:` must build a fresh client and never close the shared

    cached singleton, so later module-level helper calls can't reuse a closed
    httpx client. `client.close()` deterministically clears the cache.
    """

    import sys

    client_module = sys.modules["lab_tracker_client.client"]
    saved_from_env = client_module.client_from_env
    saved_instance = client_module._default_client_instance

    class _Fake:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    try:
        client_module._default_client_instance = None
        client_module.client_from_env = _Fake
        proxy = client_module.client

        with proxy as fresh:
            assert isinstance(fresh, _Fake)
            # The shared cache is untouched by context-manager use.
            assert client_module._default_client_instance is None
        assert fresh.closed is True

        cached = _Fake()
        client_module._default_client_instance = cached
        proxy.close()
        assert cached.closed is True
        assert client_module._default_client_instance is None
    finally:
        client_module.client_from_env = saved_from_env
        client_module._default_client_instance = saved_instance


def test_module_default_client_is_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAB_TRACKER_HTTP_TIMEOUT", "not-a-float")
    client_module = importlib.import_module("lab_tracker_client.client")
    # reload() re-executes the module in place, replacing every class object.
    # Restore the original namespace afterwards or the rest of the session sees
    # two generations of LTValidationError/LabTracker/... whose isinstance and
    # except semantics silently diverge (late imports get the new classes while
    # lab_tracker_client.repo/hpc/watch keep raising the old ones).
    saved_namespace = dict(client_module.__dict__)
    try:
        client_module = importlib.reload(client_module)

        calls = 0

        def fail_from_env() -> LabTracker:
            nonlocal calls
            calls += 1
            raise LTAPIError("constructed")

        # Set directly (not via monkeypatch): its teardown runs after the
        # finally below and would re-pollute the restored namespace.
        client_module._default_client_instance = None
        client_module.client_from_env = fail_from_env

        assert calls == 0
        with pytest.raises(LTAPIError, match="constructed"):
            client_module.health()
        assert calls == 1
    finally:
        client_module.__dict__.clear()
        client_module.__dict__.update(saved_namespace)
