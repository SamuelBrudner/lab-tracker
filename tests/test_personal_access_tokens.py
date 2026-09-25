"""Unit tests for lpat_ personal access tokens."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from lab_tracker.auth import (
    LPAT_TOKEN_PREFIX,
    PAT_SCOPE_ALL,
    PAT_SCOPE_BATCH_RUN_DUE,
    PAT_SCOPE_STAGE_EVIDENCE,
    PERSONAL_ACCESS_TOKEN_MAX_TTL,
    AuthContext,
    AuthService,
    PersonalAccessTokenService,
    PrincipalType,
    Role,
    _as_utc,
    service_principal_can_access,
    utc_now,
)
from lab_tracker.db_models import PersonalAccessTokenModel
from lab_tracker.errors import NotFoundError, ValidationError


@pytest.fixture()
def session_factory(migrated_sqlite_database_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        migrated_sqlite_database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    try:
        yield factory
    finally:
        engine.dispose()


def _services(
    session_factory: sessionmaker[Session],
) -> tuple[AuthService, PersonalAccessTokenService]:
    return AuthService(session_factory=session_factory), PersonalAccessTokenService(
        session_factory=session_factory
    )


def test_issue_token_returns_secret_once_and_hashes_storage(session_factory):
    auth_service, pat_service = _services(session_factory)
    user = auth_service.register_user("viewer", "secret", Role.VIEWER)

    issued = pat_service.issue_token(
        user,
        label="Copilot",
        role=Role.ADMIN,
        read_only=True,
        expires_at=utc_now() + timedelta(days=7),
    )

    assert issued.secret.startswith(LPAT_TOKEN_PREFIX)
    assert issued.token.role == Role.VIEWER
    assert issued.token.read_only is True
    with session_factory() as session:
        row = session.scalar(select(PersonalAccessTokenModel))
        assert row is not None
        assert row.token_hash != issued.secret
        assert len(row.token_hash) == 64


def test_issue_token_rejects_invalid_ttl_and_unknown_user(session_factory):
    auth_service, pat_service = _services(session_factory)
    user = auth_service.register_user("admin", "secret", Role.ADMIN)

    with pytest.raises(ValidationError, match="future"):
        pat_service.issue_token(
            user,
            label="expired",
            role=Role.VIEWER,
            expires_at=utc_now() - timedelta(seconds=1),
        )
    with pytest.raises(ValidationError, match="maximum"):
        pat_service.issue_token(
            user,
            label="too long",
            role=Role.VIEWER,
            expires_at=utc_now() + PERSONAL_ACCESS_TOKEN_MAX_TTL + timedelta(seconds=5),
        )
    with pytest.raises(NotFoundError):
        pat_service.issue_token(
            user.__class__(
                user_id=uuid4(),
                username="missing",
                password_hash="x",
                role=Role.ADMIN,
            ),
            label="missing",
            role=Role.ADMIN,
            expires_at=utc_now() + timedelta(days=1),
        )


def test_verify_token_returns_capped_role_and_throttles_last_used(session_factory):
    auth_service, pat_service = _services(session_factory)
    user = auth_service.register_user("editor", "secret", Role.EDITOR)
    issued = pat_service.issue_token(
        user,
        label="Notebook",
        role=Role.EDITOR,
        read_only=False,
        expires_at=utc_now() + timedelta(days=1),
    )

    principal = pat_service.verify_token(issued.secret)

    assert principal is not None
    assert principal.user_id == user.user_id
    assert principal.role == Role.EDITOR
    assert principal.read_only is False

    recent_last_used_at = utc_now() - timedelta(minutes=1)
    with session_factory() as session:
        row = session.get(PersonalAccessTokenModel, str(issued.token.token_id))
        assert row is not None
        row.last_used_at = recent_last_used_at
        session.commit()

    assert pat_service.verify_token(issued.secret) is not None
    with session_factory() as session:
        row = session.get(PersonalAccessTokenModel, str(issued.token.token_id))
        assert row is not None
        assert _as_utc(row.last_used_at) == recent_last_used_at


def test_verify_token_rejects_revoked_expired_unknown_and_malformed(session_factory):
    auth_service, pat_service = _services(session_factory)
    user = auth_service.register_user("admin", "secret", Role.ADMIN)
    issued = pat_service.issue_token(
        user,
        label="Copilot",
        role=Role.VIEWER,
        expires_at=utc_now() + timedelta(days=1),
    )

    assert pat_service.verify_token(None) is None
    assert pat_service.verify_token("") is None
    assert pat_service.verify_token("not-a-token") is None
    assert pat_service.verify_token(f"{LPAT_TOKEN_PREFIX}missing") is None

    pat_service.revoke_token(user.user_id, issued.token.token_id)
    assert pat_service.verify_token(issued.secret) is None

    expired = pat_service.issue_token(
        user,
        label="Expired soon",
        role=Role.VIEWER,
        expires_at=utc_now() + timedelta(minutes=1),
    )
    with session_factory() as session:
        row = session.get(PersonalAccessTokenModel, str(expired.token.token_id))
        assert row is not None
        row.expires_at = utc_now() - timedelta(seconds=1)
        session.commit()
    assert pat_service.verify_token(expired.secret) is None


def test_list_and_revoke_are_user_scoped(session_factory):
    auth_service, pat_service = _services(session_factory)
    owner = auth_service.register_user("owner", "secret", Role.ADMIN)
    stranger = auth_service.register_user("stranger", "secret", Role.ADMIN)
    issued = pat_service.issue_token(
        owner,
        label="Owner token",
        role=Role.ADMIN,
        expires_at=utc_now() + timedelta(days=1),
    )

    assert [token.token_id for token in pat_service.list_tokens(owner.user_id)] == [
        issued.token.token_id
    ]
    assert pat_service.list_tokens(stranger.user_id) == []
    with pytest.raises(NotFoundError):
        pat_service.revoke_token(stranger.user_id, issued.token.token_id)


def test_service_principal_policy_is_read_only_by_default():
    assert service_principal_can_access(
        "GET", "/projects", read_only=True, role=Role.VIEWER
    )
    assert not service_principal_can_access(
        "GET", "/auth/me", read_only=True, role=Role.ADMIN
    )
    assert not service_principal_can_access(
        "POST", "/projects", read_only=True, role=Role.ADMIN
    )
    assert service_principal_can_access(
        "POST", "/batches/run-due", read_only=True, role=Role.ADMIN
    )
    assert not service_principal_can_access(
        "POST", "/batches/run-due", read_only=True, role=Role.VIEWER
    )
    assert not service_principal_can_access(
        "POST", "/projects", read_only=False, role=Role.VIEWER
    )
    assert service_principal_can_access(
        "POST", "/projects", read_only=False, role=Role.EDITOR
    )


@pytest.mark.parametrize("role", (Role.VIEWER, Role.EDITOR, Role.ADMIN))
def test_read_only_all_scope_allows_exact_external_artifact_semantic_read(
    role: Role,
) -> None:
    assert service_principal_can_access(
        "POST",
        "/external-artifacts/resolve",
        read_only=True,
        role=role,
        scope=PAT_SCOPE_ALL,
    )


@pytest.mark.parametrize("role", (Role.VIEWER, Role.EDITOR, Role.ADMIN))
def test_read_only_all_scope_allows_exact_decision_context_semantic_read(
    role: Role,
) -> None:
    assert service_principal_can_access(
        "POST",
        "/assistant/decision-context",
        read_only=True,
        role=role,
        scope=PAT_SCOPE_ALL,
    )


@pytest.mark.parametrize(
    ("role", "expected"),
    (
        (Role.VIEWER, False),
        (Role.EDITOR, True),
        (Role.ADMIN, True),
    ),
)
def test_external_artifact_semantic_read_preserves_write_enabled_role_policy(
    role: Role,
    expected: bool,
) -> None:
    assert (
        service_principal_can_access(
            "POST",
            "/external-artifacts/resolve",
            read_only=False,
            role=role,
            scope=PAT_SCOPE_ALL,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("role", "expected"),
    (
        (Role.VIEWER, False),
        (Role.EDITOR, True),
        (Role.ADMIN, True),
    ),
)
def test_decision_context_semantic_read_preserves_write_enabled_role_policy(
    role: Role,
    expected: bool,
) -> None:
    assert (
        service_principal_can_access(
            "POST",
            "/assistant/decision-context",
            read_only=False,
            role=role,
            scope=PAT_SCOPE_ALL,
        )
        is expected
    )


@pytest.mark.parametrize(
    "path",
    (
        "/projects",
        "/api/external-artifacts/resolve",
        "/external-artifacts/resolve/",
    ),
)
def test_read_only_external_artifact_exception_rejects_post_near_misses(
    path: str,
) -> None:
    assert not service_principal_can_access(
        "POST",
        path,
        read_only=True,
        role=Role.ADMIN,
        scope=PAT_SCOPE_ALL,
    )


@pytest.mark.parametrize(
    "path",
    (
        "/assistant",
        "/api/assistant/decision-context",
        "/assistant/decision-context/",
    ),
)
def test_read_only_decision_context_exception_rejects_post_near_misses(
    path: str,
) -> None:
    assert not service_principal_can_access(
        "POST",
        path,
        read_only=True,
        role=Role.ADMIN,
        scope=PAT_SCOPE_ALL,
    )


@pytest.mark.parametrize("read_only", (True, False))
@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("GET", "/auth"),
        ("GET", "/auth/me"),
        ("POST", "/auth/tokens"),
    ),
)
def test_all_scope_keeps_auth_paths_fail_closed(
    method: str,
    path: str,
    read_only: bool,
) -> None:
    assert not service_principal_can_access(
        method,
        path,
        read_only=read_only,
        role=Role.ADMIN,
        scope=PAT_SCOPE_ALL,
    )


def test_batch_run_due_scope_allows_only_the_run_due_post():
    # The scheduler scope must not permit anything except triggering the run —
    # not reads, not other writes, not /auth — regardless of read_only.
    assert service_principal_can_access(
        "POST", "/batches/run-due", read_only=True, role=Role.ADMIN, scope=PAT_SCOPE_BATCH_RUN_DUE
    )
    assert not service_principal_can_access(
        "GET", "/projects", read_only=True, role=Role.ADMIN, scope=PAT_SCOPE_BATCH_RUN_DUE
    )
    assert not service_principal_can_access(
        "GET", "/batches/runs", read_only=True, role=Role.ADMIN, scope=PAT_SCOPE_BATCH_RUN_DUE
    )
    assert not service_principal_can_access(
        "POST",
        "/external-artifacts/resolve",
        read_only=True,
        role=Role.ADMIN,
        scope=PAT_SCOPE_BATCH_RUN_DUE,
    )
    assert not service_principal_can_access(
        "POST",
        "/assistant/decision-context",
        read_only=True,
        role=Role.ADMIN,
        scope=PAT_SCOPE_BATCH_RUN_DUE,
    )
    assert not service_principal_can_access(
        "POST", "/batches/run-now", read_only=False, role=Role.ADMIN, scope=PAT_SCOPE_BATCH_RUN_DUE
    )
    assert not service_principal_can_access(
        "GET", "/auth/me", read_only=True, role=Role.ADMIN, scope=PAT_SCOPE_BATCH_RUN_DUE
    )
    # Still gated on the admin role the daily review requires.
    assert not service_principal_can_access(
        "POST", "/batches/run-due", read_only=True, role=Role.EDITOR, scope=PAT_SCOPE_BATCH_RUN_DUE
    )


@pytest.mark.parametrize(
    ("method", "path", "read_only"),
    (
        ("GET", "/projects", True),
        ("POST", "/batches/run-due", True),
        ("POST", "/external-artifacts/resolve", True),
        ("POST", "/assistant/decision-context", True),
        ("POST", "/projects", False),
    ),
)
def test_unknown_service_principal_scopes_fail_closed_before_every_allowance(
    method: str,
    path: str,
    read_only: bool,
) -> None:
    assert not service_principal_can_access(
        method,
        path,
        read_only=read_only,
        role=Role.ADMIN,
        scope="future_semantic_read",
    )


@pytest.mark.parametrize(
    ("method", "path", "read_only"),
    (
        ("GET", "/projects", True),
        ("POST", "/batches/run-due", True),
        ("POST", "/external-artifacts/resolve", True),
        ("POST", "/assistant/decision-context", True),
        ("POST", "/projects", False),
    ),
)
def test_registered_but_unhandled_future_scopes_remain_fail_closed(
    method: str,
    path: str,
    read_only: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    future_scope = "future_semantic_read"
    monkeypatch.setattr(
        "lab_tracker.auth.PAT_SCOPES",
        frozenset(
            {
                PAT_SCOPE_ALL,
                PAT_SCOPE_BATCH_RUN_DUE,
                future_scope,
            }
        ),
    )

    assert not service_principal_can_access(
        method,
        path,
        read_only=read_only,
        role=Role.ADMIN,
        scope=future_scope,
    )


def test_issue_token_records_scope_and_defaults_to_all(session_factory):
    auth_service, pat_service = _services(session_factory)
    admin = auth_service.register_user("admin", "secret", Role.ADMIN)

    default_token = pat_service.issue_token(
        admin, label="Default", role=Role.ADMIN, expires_at=utc_now() + timedelta(days=7)
    )
    assert default_token.token.scope == PAT_SCOPE_ALL

    scoped = pat_service.issue_token(
        admin,
        label="Scheduler",
        role=Role.ADMIN,
        scope=PAT_SCOPE_BATCH_RUN_DUE,
        expires_at=utc_now() + timedelta(days=7),
    )
    assert scoped.token.scope == PAT_SCOPE_BATCH_RUN_DUE

    principal = pat_service.verify_token(scoped.secret)
    assert principal is not None
    assert principal.scope == PAT_SCOPE_BATCH_RUN_DUE


def test_issue_token_rejects_an_unknown_scope(session_factory):
    auth_service, pat_service = _services(session_factory)
    admin = auth_service.register_user("admin", "secret", Role.ADMIN)
    with pytest.raises(ValidationError):
        pat_service.issue_token(
            admin,
            label="Bad scope",
            role=Role.ADMIN,
            scope="everything",
            expires_at=utc_now() + timedelta(days=7),
        )


def uuid_from(value: object) -> UUID:
    return UUID(str(value))


def test_verify_token_narrows_role_to_the_live_user_role_after_demotion(session_factory):
    auth_service, pat_service = _services(session_factory)
    auth_service.register_user("root", "secret", Role.ADMIN)
    demoted = auth_service.register_user("demoted", "secret", Role.ADMIN)
    issued = pat_service.issue_token(
        demoted,
        label="Agent",
        role=Role.ADMIN,
        read_only=False,
        scope=PAT_SCOPE_BATCH_RUN_DUE,
        expires_at=utc_now() + timedelta(days=1),
    )
    before = pat_service.verify_token(issued.secret)
    assert before is not None
    assert before.role == Role.ADMIN

    auth_service.update_user(demoted.user_id, role=Role.VIEWER)

    after = pat_service.verify_token(issued.secret)
    assert after is not None
    assert after.role == Role.VIEWER
    # The stored issuance cap is unchanged; only the effective role narrows.
    assert pat_service.list_tokens(demoted.user_id)[0].role == Role.ADMIN
    assert not service_principal_can_access(
        "POST",
        "/batches/run-due",
        read_only=after.read_only,
        role=after.role,
        scope=after.scope,
    )

    # Re-promotion restores at most the issuance-time cap, never more.
    auth_service.update_user(demoted.user_id, role=Role.ADMIN)
    restored = pat_service.verify_token(issued.secret)
    assert restored is not None
    assert restored.role == Role.ADMIN


def test_verify_token_rejects_a_token_whose_user_no_longer_exists(session_factory):
    auth_service, pat_service = _services(session_factory)
    user = auth_service.register_user("gone", "secret", Role.EDITOR)
    issued = pat_service.issue_token(
        user,
        label="Orphan",
        role=Role.EDITOR,
        expires_at=utc_now() + timedelta(days=1),
    )
    with session_factory() as session:
        session.execute(text("PRAGMA foreign_keys=OFF"))
        session.execute(
            text("DELETE FROM users WHERE user_id = :user_id"),
            {"user_id": str(user.user_id)},
        )
        session.commit()

    assert pat_service.verify_token(issued.secret) is None



STAGE_NOTE_ID = "0f6a3c1e-2b4d-4c8e-9a1f-3d5e7b9c1a2b"


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("GET", "/projects"),
        ("GET", f"/notes/{STAGE_NOTE_ID}"),
        ("HEAD", "/health"),
        ("POST", "/notes"),
        ("POST", "/notes/upload-file"),
        ("POST", "/notes/quick-capture"),
        ("POST", "/evidence-bundles"),
        ("POST", f"/notes/{STAGE_NOTE_ID}/graph-drafts"),
        ("POST", f"/notes/{STAGE_NOTE_ID}/analysis-graph-drafts"),
        ("POST", f"/notes/{STAGE_NOTE_ID}/transcript"),
        ("PATCH", f"/notes/{STAGE_NOTE_ID}"),
        ("POST", "/assistant/decision-context"),
        ("POST", "/external-artifacts/resolve"),
    ),
)
def test_stage_evidence_scope_allows_reads_captures_drafts_and_note_patches(
    method: str,
    path: str,
) -> None:
    assert service_principal_can_access(
        method, path, read_only=False, role=Role.EDITOR, scope=PAT_SCOPE_STAGE_EVIDENCE
    )


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("POST", "/projects"),
        ("POST", "/questions"),
        ("POST", "/datasets"),
        ("POST", "/analyses"),
        ("POST", "/claims"),
        ("POST", "/visualizations"),
        ("POST", "/goals"),
        ("POST", "/batches/run-due"),
        ("POST", "/batches/run-now"),
        ("POST", f"/graph-drafts/{STAGE_NOTE_ID}/commit"),
        ("POST", f"/graph-drafts/{STAGE_NOTE_ID}/accept"),
        ("PATCH", f"/questions/{STAGE_NOTE_ID}"),
        ("DELETE", f"/notes/{STAGE_NOTE_ID}"),
        ("POST", f"/notes/{STAGE_NOTE_ID}/archive"),
        ("GET", "/auth/me"),
        ("POST", "/auth/tokens"),
    ),
)
def test_stage_evidence_scope_denies_every_other_write(method: str, path: str) -> None:
    # Even the strongest role and a write-enabled token stay inside the allow-list.
    assert not service_principal_can_access(
        method, path, read_only=False, role=Role.ADMIN, scope=PAT_SCOPE_STAGE_EVIDENCE
    )


@pytest.mark.parametrize(
    ("read_only", "role"),
    ((True, Role.EDITOR), (True, Role.ADMIN), (False, Role.VIEWER)),
    ids=["read-only-editor", "read-only-admin", "write-enabled-viewer"],
)
def test_stage_evidence_scope_respects_read_only_and_viewer_role(
    read_only: bool,
    role: Role,
) -> None:
    for method, path in (
        ("GET", "/projects"),
        ("POST", "/assistant/decision-context"),
        ("POST", "/external-artifacts/resolve"),
    ):
        assert service_principal_can_access(
            method, path, read_only=read_only, role=role, scope=PAT_SCOPE_STAGE_EVIDENCE
        ), (method, path)
    for method, path in (
        ("POST", "/notes"),
        ("POST", "/notes/quick-capture"),
        ("POST", "/evidence-bundles"),
        ("POST", f"/notes/{STAGE_NOTE_ID}/graph-drafts"),
        ("PATCH", f"/notes/{STAGE_NOTE_ID}"),
    ):
        assert not service_principal_can_access(
            method, path, read_only=read_only, role=role, scope=PAT_SCOPE_STAGE_EVIDENCE
        ), (method, path)


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("POST", f"/notes/{STAGE_NOTE_ID}/graph-drafts/extra"),
        ("POST", f"/notes/{STAGE_NOTE_ID}/graph-drafts/"),
        ("POST", "/notesx"),
        ("POST", "/notes/"),
        ("POST", "/notes//graph-drafts"),
        ("POST", f"//notes/{STAGE_NOTE_ID}/graph-drafts"),
        ("POST", f"/notes/{STAGE_NOTE_ID}/commit"),
        ("PATCH", "/notes/"),
        ("PATCH", f"/notes/{STAGE_NOTE_ID}/transcript"),
        ("PATCH", f"/questions/{STAGE_NOTE_ID}"),
        ("PUT", f"/notes/{STAGE_NOTE_ID}"),
    ),
)
def test_stage_evidence_scope_rejects_near_miss_note_paths(method: str, path: str) -> None:
    assert not service_principal_can_access(
        method, path, read_only=False, role=Role.EDITOR, scope=PAT_SCOPE_STAGE_EVIDENCE
    )


def test_issue_token_records_stage_evidence_scope(session_factory):
    auth_service, pat_service = _services(session_factory)
    admin = auth_service.register_user("admin", "secret", Role.ADMIN)

    issued = pat_service.issue_token(
        admin,
        label="Capture hook",
        role=Role.EDITOR,
        read_only=False,
        scope=PAT_SCOPE_STAGE_EVIDENCE,
        expires_at=utc_now() + timedelta(days=7),
    )

    assert issued.token.scope == PAT_SCOPE_STAGE_EVIDENCE
    principal = pat_service.verify_token(issued.secret)
    assert principal is not None
    assert principal.scope == "stage_evidence"
    assert principal.label == "Capture hook"


def test_auth_context_reports_stage_evidence_scope() -> None:
    user_id = uuid4()
    stage_scoped = AuthContext(
        user_id=user_id,
        role=Role.EDITOR,
        principal_type=PrincipalType.SERVICE,
        principal_label="Capture hook",
        service_scope=PAT_SCOPE_STAGE_EVIDENCE,
    )
    all_scoped = AuthContext(
        user_id=user_id,
        role=Role.EDITOR,
        principal_type=PrincipalType.SERVICE,
        principal_label="Agent",
        service_scope=PAT_SCOPE_ALL,
    )
    # The flag is about the presenting credential: a browser session never
    # carries a scope, and a stray scope value on a USER principal is inert.
    browser = AuthContext(user_id=user_id, role=Role.EDITOR)
    mislabelled_user = AuthContext(
        user_id=user_id, role=Role.EDITOR, service_scope=PAT_SCOPE_STAGE_EVIDENCE
    )

    assert stage_scoped.is_stage_evidence_scoped is True
    assert all_scoped.is_stage_evidence_scoped is False
    assert browser.is_stage_evidence_scoped is False
    assert mislabelled_user.is_stage_evidence_scoped is False
