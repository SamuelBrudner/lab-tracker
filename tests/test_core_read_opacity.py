from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from read_opacity_inventory import (
    CORE_READ_OPACITY_VARIANTS,
    CORE_SUITE,
    READ_OPACITY_VARIANTS_BY_ID,
)
from sqlalchemy import select

from lab_tracker.auth import utc_now
from lab_tracker.db_models import UsageEventModel
from lab_tracker.models import encode_session_link_code
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


@dataclass(frozen=True)
class CoreReadRecords:
    project_id: str
    question_id: str
    note_id: str
    plain_note_id: str
    session_id: str
    session_link_code: str
    missing_project_id: str
    missing_question_id: str
    missing_note_id: str
    missing_session_id: str
    missing_session_link_code: str


@dataclass(frozen=True)
class ReadCase:
    name: str
    existing_path: str
    missing_path: str
    not_found_label: str
    media_type: str
    authorized_value: Callable[[Any], object]
    expected_value: object
    accept: str = "application/json"


CORE_READ_DOMAINS = ("projects", "questions", "notes", "sessions")


def _create_question(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
) -> str:
    response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Which core read should remain opaque?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    question_id = response.json()["data"]["question_id"]
    updated = client.patch(
        f"/questions/{question_id}",
        json={"text": "Which core read is now opaque?"},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    return question_id


def _upload_note(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
) -> str:
    response = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("opaque.txt", b"opaque-note-bytes", "text/plain")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["note_id"]


def _create_plain_note(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
) -> str:
    response = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "No uploaded raw asset."},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["note_id"]


def _create_session(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
) -> tuple[str, str]:
    response = client.post(
        "/sessions",
        json={"project_id": project_id, "session_type": "operational"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    payload = response.json()["data"]
    return payload["session_id"], payload["link_code"]


@pytest.fixture()
def core_read_records(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
) -> CoreReadRecords:
    project_id = scoped_project_member.hidden_project_id
    question_id = _create_question(client, admin_auth_headers, project_id)
    note_id = _upload_note(client, admin_auth_headers, project_id)
    plain_note_id = _create_plain_note(client, admin_auth_headers, project_id)
    session_id, session_link_code = _create_session(
        client,
        admin_auth_headers,
        project_id,
    )
    missing_session_id = str(uuid4())
    return CoreReadRecords(
        project_id=project_id,
        question_id=question_id,
        note_id=note_id,
        plain_note_id=plain_note_id,
        session_id=session_id,
        session_link_code=session_link_code,
        missing_project_id=str(uuid4()),
        missing_question_id=str(uuid4()),
        missing_note_id=str(uuid4()),
        missing_session_id=missing_session_id,
        missing_session_link_code=encode_session_link_code(
            uuid4()
        ),
    )


def _read_cases(records: CoreReadRecords) -> dict[str, tuple[ReadCase, ...]]:
    project_id = records.project_id
    question_id = records.question_id
    note_id = records.note_id
    session_id = records.session_id
    missing_project_id = records.missing_project_id
    missing_question_id = records.missing_question_id
    missing_note_id = records.missing_note_id
    missing_session_id = records.missing_session_id
    return {
        "projects": (
            ReadCase(
                "project-detail",
                f"/projects/{project_id}",
                f"/projects/{missing_project_id}",
                "Project",
                "application/json",
                lambda response: response.json()["data"]["project_id"],
                project_id,
            ),
            ReadCase(
                "publication-readiness",
                f"/projects/{project_id}/publication-readiness",
                f"/projects/{missing_project_id}/publication-readiness",
                "Project",
                "application/json",
                lambda response: response.json()["data"]["project_id"],
                project_id,
            ),
            ReadCase(
                "project-graph-json",
                f"/projects/{project_id}/graph?view=evidence",
                f"/projects/{missing_project_id}/graph?view=evidence",
                "Project",
                "application/json",
                lambda response: response.json()["data"]["project_id"],
                project_id,
            ),
            ReadCase(
                "project-graph-overview",
                f"/projects/{project_id}/graph/overview",
                f"/projects/{missing_project_id}/graph/overview",
                "Project",
                "application/json",
                lambda response: response.json()["data"]["project"]["project_id"],
                project_id,
            ),
            ReadCase(
                "project-graph-search",
                f"/projects/{project_id}/graph/search?q=opaque",
                f"/projects/{missing_project_id}/graph/search?q=opaque",
                "Project",
                "application/json",
                lambda response: response.json()["data"]["project_id"],
                project_id,
            ),
            ReadCase(
                "project-graph-neighborhood",
                f"/projects/{project_id}/graph/neighborhood/question/{question_id}",
                (
                    f"/projects/{missing_project_id}/graph/neighborhood/question/"
                    f"{question_id}"
                ),
                "Project",
                "application/json",
                lambda response: response.json()["data"]["anchor"]["entity_id"],
                question_id,
            ),
            ReadCase(
                "project-graph-mermaid",
                f"/projects/{project_id}/graph/mermaid?view=evidence",
                f"/projects/{missing_project_id}/graph/mermaid?view=evidence",
                "Project",
                "text/vnd.mermaid",
                lambda response: response.text.startswith("graph LR\n"),
                True,
                accept="text/vnd.mermaid",
            ),
        ),
        "questions": (
            ReadCase(
                "question-detail",
                f"/questions/{question_id}",
                f"/questions/{missing_question_id}",
                "Question",
                "application/json",
                lambda response: response.json()["data"]["question_id"],
                question_id,
            ),
            ReadCase(
                "question-versions",
                f"/questions/{question_id}/versions?limit=50&offset=0",
                f"/questions/{missing_question_id}/versions?limit=50&offset=0",
                "Question",
                "application/json",
                lambda response: response.json()["meta"]["total"],
                2,
            ),
            ReadCase(
                "question-version-diff",
                (
                    f"/questions/{question_id}/versions/diff"
                    "?from_version=1&to_version=2"
                ),
                (
                    f"/questions/{missing_question_id}/versions/diff"
                    "?from_version=1&to_version=2"
                ),
                "Question",
                "application/json",
                lambda response: response.json()["data"]["changed_fields"]["text"],
                {
                    "before": "Which core read should remain opaque?",
                    "after": "Which core read is now opaque?",
                },
            ),
            ReadCase(
                "question-refactors",
                f"/questions/{question_id}/refactors?limit=50&offset=0",
                f"/questions/{missing_question_id}/refactors?limit=50&offset=0",
                "Question",
                "application/json",
                lambda response: response.json()["data"],
                [],
            ),
            ReadCase(
                "question-ara-artifact",
                f"/questions/{question_id}/ara-artifact",
                f"/questions/{missing_question_id}/ara-artifact",
                "Question",
                "application/ld+json",
                lambda response: response.json()["@id"],
                f"http://testserver/questions/{question_id}/ara-artifact",
                accept="application/ld+json",
            ),
            ReadCase(
                "question-ara-layer",
                f"/questions/{question_id}/ara-artifact/evidence",
                f"/questions/{missing_question_id}/ara-artifact/evidence",
                "Question",
                "application/ld+json",
                lambda response: response.json()["@id"],
                (
                    f"http://testserver/questions/{question_id}"
                    "/ara-artifact/evidence"
                ),
                accept="application/ld+json",
            ),
        ),
        "notes": (
            ReadCase(
                "note-detail",
                f"/notes/{note_id}",
                f"/notes/{missing_note_id}",
                "Note",
                "application/json",
                lambda response: response.json()["data"]["note_id"],
                note_id,
            ),
            ReadCase(
                "note-raw",
                f"/notes/{note_id}/raw",
                f"/notes/{missing_note_id}/raw",
                "Note",
                "application/json",
                lambda response: base64.b64decode(
                    response.json()["data"]["content_base64"]
                ),
                b"opaque-note-bytes",
            ),
        ),
        "sessions": (
            ReadCase(
                "session-detail",
                f"/sessions/{session_id}",
                f"/sessions/{missing_session_id}",
                "Session",
                "application/json",
                lambda response: response.json()["data"]["session_id"],
                session_id,
            ),
            ReadCase(
                "session-by-link",
                f"/sessions/by-link/{records.session_link_code}",
                f"/sessions/by-link/{records.missing_session_link_code}",
                "Session",
                "application/json",
                lambda response: response.json()["data"]["session_id"],
                session_id,
            ),
            ReadCase(
                "session-outputs",
                f"/sessions/{session_id}/outputs?limit=50&offset=0",
                f"/sessions/{missing_session_id}/outputs?limit=50&offset=0",
                "Session",
                "application/json",
                lambda response: response.json()["data"],
                [],
            ),
        ),
    }


def _request_headers(
    auth_headers: dict[str, str],
    case: ReadCase,
) -> dict[str, str]:
    return {**auth_headers, "Accept": case.accept}


def _not_found_body(label: str) -> dict[str, object]:
    return {
        "error": {
            "code": "not_found",
            "message": f"{label} does not exist.",
            "issues": None,
        }
    }


@pytest.mark.parametrize("domain", CORE_READ_DOMAINS)
def test_core_read_variants_are_opaque_and_preserve_authorized_contracts(
    domain: str,
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
    core_read_records: CoreReadRecords,
) -> None:
    cases_by_domain = _read_cases(core_read_records)
    assert tuple(cases_by_domain) == CORE_READ_DOMAINS
    assert sum(len(cases) for cases in cases_by_domain.values()) == 18
    inventory_coverage_ids = {
        variant.coverage_id for variant in CORE_READ_OPACITY_VARIANTS
    }
    assert {
        f"{CORE_SUITE}.{case.name}"
        for cases in cases_by_domain.values()
        for case in cases
    } == inventory_coverage_ids

    for case in cases_by_domain[domain]:
        coverage_id = f"{CORE_SUITE}.{case.name}"
        assert coverage_id in inventory_coverage_ids
        inventory_variant = READ_OPACITY_VARIANTS_BY_ID[coverage_id]
        assert inventory_variant.matches_request(
            method="GET",
            request_target=case.existing_path,
            variant="default",
        )
        assert inventory_variant.matches_request(
            method="GET",
            request_target=case.missing_path,
            variant="default",
        )
        authorized = client.get(
            case.existing_path,
            headers=_request_headers(admin_auth_headers, case),
        )
        assert authorized.status_code == 200, f"{case.name}: {authorized.text}"
        assert authorized.headers["content-type"].startswith(case.media_type), case.name
        assert case.authorized_value(authorized) == case.expected_value, case.name

        outsider_existing = client.get(
            case.existing_path,
            headers=_request_headers(scoped_project_member.member_headers, case),
        )
        outsider_missing = client.get(
            case.missing_path,
            headers=_request_headers(scoped_project_member.member_headers, case),
        )

        assert outsider_existing.status_code == outsider_missing.status_code == 404, (
            case.name,
            outsider_existing.text,
            outsider_missing.text,
        )
        assert outsider_existing.json() == outsider_missing.json() == _not_found_body(
            case.not_found_label
        ), case.name


def test_all_core_read_variants_still_require_authentication(
    client: TestClient,
    core_read_records: CoreReadRecords,
) -> None:
    cases = [
        case
        for domain_cases in _read_cases(core_read_records).values()
        for case in domain_cases
    ]
    assert len(cases) == 18

    for case in cases:
        response = client.get(case.existing_path, headers={"Accept": case.accept})
        assert response.status_code == 401, f"{case.name}: {response.text}"
        assert response.json()["error"] == {
            "code": "auth_error",
            "message": "Missing Authorization header.",
            "issues": None,
        }, case.name


def test_invalid_credentials_and_scoped_capabilities_keep_transport_statuses(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    core_read_records: CoreReadRecords,
) -> None:
    invalid = client.get(
        f"/projects/{core_read_records.project_id}",
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "auth_error"

    issued = client.post(
        "/auth/tokens",
        json={
            "label": "Opaque read capability test",
            "role": "admin",
            "read_only": False,
            "scope": "batch_run_due",
            "expires_at": (utc_now() + timedelta(days=1)).isoformat(),
        },
        headers=admin_auth_headers,
    )
    assert issued.status_code == 201, issued.text
    scoped_headers = {
        "Authorization": f"Bearer {issued.json()['data']['secret']}"
    }
    forbidden = client.get(
        f"/projects/{core_read_records.project_id}",
        headers=scoped_headers,
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"] == {
        "code": "service_forbidden",
        "message": "Not permitted for this token.",
        "issues": None,
    }


def test_group_inherited_reader_can_use_the_opaque_project_boundary(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
) -> None:
    group_response = client.post(
        "/groups",
        json={
            "name": "Opaque group-read boundary",
            "group_read_all": True,
        },
        headers=admin_auth_headers,
    )
    assert group_response.status_code == 201, group_response.text
    group_id = group_response.json()["data"]["group_id"]

    project_response = client.post(
        "/projects",
        json={
            "name": "Group-readable opaque project",
            "group_id": group_id,
        },
        headers=admin_auth_headers,
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["data"]["project_id"]

    membership_response = client.post(
        f"/groups/{group_id}/members",
        json={
            "username": scoped_project_member.member_username,
            "role": "viewer",
        },
        headers=admin_auth_headers,
    )
    assert membership_response.status_code == 201, membership_response.text

    response = client.get(
        f"/projects/{project_id}",
        headers=scoped_project_member.member_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["project_id"] == project_id


def test_core_mutations_keep_permission_errors_instead_of_becoming_opaque(
    client: TestClient,
    scoped_project_member,
    core_read_records: CoreReadRecords,
) -> None:
    cases: tuple[tuple[str, str, dict[str, object] | None], ...] = (
        ("PATCH", f"/projects/{core_read_records.project_id}", {"name": "Forbidden"}),
        ("DELETE", f"/projects/{core_read_records.project_id}", None),
        (
            "PATCH",
            f"/questions/{core_read_records.question_id}",
            {"text": "Forbidden?"},
        ),
        ("DELETE", f"/questions/{core_read_records.question_id}", None),
        (
            "PATCH",
            f"/notes/{core_read_records.note_id}",
            {"transcribed_text": "Forbidden"},
        ),
        ("DELETE", f"/notes/{core_read_records.note_id}", None),
        (
            "PATCH",
            f"/sessions/{core_read_records.session_id}",
            {"status": "closed"},
        ),
        ("DELETE", f"/sessions/{core_read_records.session_id}", None),
    )

    for method, path, payload in cases:
        response = client.request(
            method,
            path,
            json=payload,
            headers=scoped_project_member.member_headers,
        )
        assert response.status_code == 401, f"{method} {path}: {response.text}"
        assert response.json()["error"]["code"] == "auth_error"


def test_validation_precedence_is_independent_of_target_visibility(
    client: TestClient,
    scoped_project_member,
    core_read_records: CoreReadRecords,
) -> None:
    pairs = (
        (
            f"/projects/{core_read_records.project_id}/graph?view=invalid",
            f"/projects/{core_read_records.missing_project_id}/graph?view=invalid",
        ),
        (
            f"/projects/{core_read_records.project_id}/graph/mermaid?view=invalid",
            (
                f"/projects/{core_read_records.missing_project_id}"
                "/graph/mermaid?view=invalid"
            ),
        ),
        (
            f"/questions/{core_read_records.question_id}/versions?limit=0",
            f"/questions/{core_read_records.missing_question_id}/versions?limit=0",
        ),
        (
            (
                f"/questions/{core_read_records.question_id}/versions/diff"
                "?from_version=invalid&to_version=2"
            ),
            (
                f"/questions/{core_read_records.missing_question_id}/versions/diff"
                "?from_version=invalid&to_version=2"
            ),
        ),
        (
            f"/questions/{core_read_records.question_id}/refactors?limit=0",
            f"/questions/{core_read_records.missing_question_id}/refactors?limit=0",
        ),
        (
            f"/sessions/{core_read_records.session_id}/outputs?limit=0",
            f"/sessions/{core_read_records.missing_session_id}/outputs?limit=0",
        ),
        (
            f"/questions/{core_read_records.question_id}/ara-artifact/not-a-layer",
            (
                f"/questions/{core_read_records.missing_question_id}"
                "/ara-artifact/not-a-layer"
            ),
        ),
    )

    for existing_path, missing_path in pairs:
        existing = client.get(
            existing_path,
            headers=scoped_project_member.member_headers,
        )
        missing = client.get(
            missing_path,
            headers=scoped_project_member.member_headers,
        )
        assert existing.status_code == missing.status_code == 422, (
            existing_path,
            existing.text,
            missing.text,
        )
        assert existing.json() == missing.json(), existing_path

    invalid_link_member = client.get(
        "/sessions/by-link/not-a-valid-link",
        headers=scoped_project_member.member_headers,
    )
    assert invalid_link_member.status_code == 422
    assert invalid_link_member.json()["error"]["message"] == (
        "Invalid session link code."
    )


@pytest.mark.parametrize(
    ("path", "expected_status"),
    (
        ("/projects/not-a-uuid", 422),
        ("/questions/not-a-uuid", 422),
        ("/sessions/not-a-uuid", 422),
        ("/questions/not-a-uuid/ara-artifact", 404),
        ("/questions/not-a-uuid/ara-artifact/evidence", 404),
        ("/notes/not-a-uuid", 404),
        ("/notes/not-a-uuid/raw", 404),
    ),
)
def test_route_specific_malformed_uuid_behavior_is_preserved(
    path: str,
    expected_status: int,
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    assert client.get(path, headers=admin_auth_headers).status_code == expected_status


def test_child_not_found_errors_are_not_rewritten_as_target_absence(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    core_read_records: CoreReadRecords,
) -> None:
    missing_version = client.get(
        (
            f"/questions/{core_read_records.question_id}/versions/diff"
            "?from_version=1&to_version=99"
        ),
        headers=admin_auth_headers,
    )
    assert missing_version.status_code == 404
    assert missing_version.json()["error"]["message"] == (
        "Entity version does not exist."
    )

    note_without_asset = client.get(
        f"/notes/{core_read_records.plain_note_id}/raw",
        headers=admin_auth_headers,
    )
    assert note_without_asset.status_code == 404
    assert note_without_asset.json()["error"]["message"] == (
        "Note does not have raw content."
    )


def test_denied_raw_note_read_never_touches_storage(
    client: TestClient,
    scoped_project_member,
    core_read_records: CoreReadRecords,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[object] = []

    def fail_if_read(storage_id) -> bytes:  # noqa: ANN001
        reads.append(storage_id)
        raise AssertionError("raw storage must not run before read authorization")

    monkeypatch.setattr(client.app.state.raw_note_storage, "read", fail_if_read)

    response = client.get(
        f"/notes/{core_read_records.note_id}/raw",
        headers={
            **scoped_project_member.member_headers,
            "Accept": "application/json",
        },
    )

    assert response.status_code == 404
    assert response.json() == _not_found_body("Note")
    assert reads == []


def test_denied_session_outputs_never_query_output_rows(
    client: TestClient,
    scoped_project_member,
    core_read_records: CoreReadRecords,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def fail_if_queried(self, *args, **kwargs):  # noqa: ANN001
        calls.append((self, args, kwargs))
        raise AssertionError("output rows must not be queried before authorization")

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "query_acquisition_outputs",
        fail_if_queried,
    )

    response = client.get(
        f"/sessions/{core_read_records.session_id}/outputs",
        headers=scoped_project_member.member_headers,
    )

    assert response.status_code == 404
    assert response.json() == _not_found_body("Session")
    assert calls == []


def test_denied_detail_reads_do_not_record_usage(
    client: TestClient,
    scoped_project_member,
    core_read_records: CoreReadRecords,
) -> None:
    client.app.state.settings.usage_events = True
    paths = (
        f"/projects/{core_read_records.project_id}",
        f"/questions/{core_read_records.question_id}",
        f"/notes/{core_read_records.note_id}",
        f"/sessions/{core_read_records.session_id}",
        f"/sessions/by-link/{core_read_records.session_link_code}",
    )

    for path in paths:
        response = client.get(path, headers=scoped_project_member.member_headers)
        assert response.status_code == 404, f"{path}: {response.text}"

    with client.app.state.db_session_factory() as session:
        events = list(
            session.scalars(
                select(UsageEventModel).where(
                    UsageEventModel.actor_user_id
                    == scoped_project_member.member_user_id
                )
            )
        )
    assert events == []
