from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from api_helpers import repository_backed_api

from lab_tracker.auth import AuthContext, Role
from lab_tracker.db_models import UserModel
from lab_tracker.errors import ConflictError, NotFoundError, ValidationError
from lab_tracker.models import (
    ClaimStatus,
    EvidenceBundleRecord,
    ProjectMembershipRole,
)
from lab_tracker.repository import EvidenceBundleKeyRaceError
from lab_tracker.services.evidence_bundle_service import (
    CreateAnalysisIntent,
    CreateClaimIntent,
    CreateDatasetIntent,
    CreateSourceNoteIntent,
    EvidenceBundleUploadIntent,
    ExistingSourceNoteIntent,
    ExistingVisualizationIntent,
    RecordEvidenceBundleCommand,
)


def _actor() -> AuthContext:
    return AuthContext(user_id=uuid4(), role=Role.ADMIN)


def _project_and_question(api, actor):  # noqa: ANN001
    project = api.create_project("Direct bundle", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Will a late bundle failure roll back?",
        question_type="descriptive",
        status="active",
        actor=actor,
    )
    return project, question


def test_direct_bundle_rolls_back_entities_after_late_execution_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = _actor()
    project, question = _project_and_question(api, actor)
    command = RecordEvidenceBundleCommand(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        dataset=CreateDatasetIntent(),
        analysis=CreateAnalysisIntent(
            dataset_ids=(),
            method_hash="method-v1",
            code_version="code-v1",
        ),
        claim=CreateClaimIntent(statement="Late failure", confidence=75),
        dry_run=False,
        idempotency_key="direct-late-failure",
    )

    def fail_claim(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("simulated late claim failure")

    monkeypatch.setattr(api.claims, "create_claim", fail_claim)
    with pytest.raises(RuntimeError, match="simulated late claim failure"):
        api.record_evidence_bundle(command, actor=actor)

    assert api.list_datasets(project_id=project.project_id) == []
    assert api.list_analyses(project_id=project.project_id) == []
    assert api.evidence_bundles.repository.evidence_bundles.list() == []


def test_unique_race_loser_fully_rolls_back_speculative_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Direct race", actor=actor)
    command = RecordEvidenceBundleCommand(
        project_id=project.project_id,
        source_note=CreateSourceNoteIntent(raw_content="Speculative loser"),
        dry_run=False,
        idempotency_key="direct-race",
    )
    fingerprint = api.evidence_bundles._fingerprint(command)  # noqa: SLF001
    winner_note_id = uuid4()
    winner = EvidenceBundleRecord(
        bundle_id=uuid4(),
        project_id=project.project_id,
        created_by=str(actor.user_id),
        idempotency_key="direct-race",
        request_fingerprint=fingerprint,
        result={
            "outcome": "created",
            "dry_run": False,
            "project_id": str(project.project_id),
            "idempotency_key": "direct-race",
            "component_ids": {
                "dataset_id": None,
                "analysis_id": None,
                "claim_id": None,
                "visualization_id": None,
                "source_note_id": str(winner_note_id),
            },
            "plan": [
                {
                    "action": "create",
                    "entity_type": "source_note",
                    "entity_id": str(winner_note_id),
                    "reason": None,
                }
            ],
            "warnings": [],
        },
    )
    bundle_repository = api.evidence_bundles.repository.evidence_bundles
    lookups = 0

    def winner_after_race(**kwargs):  # noqa: ANN003
        nonlocal lookups
        lookups += 1
        return None if lookups == 1 else winner

    def lose_unique_race(_record):  # noqa: ANN001
        raise EvidenceBundleKeyRaceError("simulated bundle-key race")

    monkeypatch.setattr(bundle_repository, "get_by_key", winner_after_race)
    monkeypatch.setattr(bundle_repository, "insert", lose_unique_race)

    result = api.record_evidence_bundle(command, actor=actor)

    assert result.outcome == "reused"
    assert result.component_ids.source_note_id == winner_note_id
    assert api.list_notes(project_id=project.project_id) == []


def test_unique_race_conflict_rolls_back_before_returning_409_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Direct conflict race", actor=actor)
    command = RecordEvidenceBundleCommand(
        project_id=project.project_id,
        source_note=CreateSourceNoteIntent(raw_content="Losing payload"),
        dry_run=False,
        idempotency_key="direct-conflict-race",
    )
    winner = EvidenceBundleRecord(
        bundle_id=uuid4(),
        project_id=project.project_id,
        created_by=str(actor.user_id),
        idempotency_key="direct-conflict-race",
        request_fingerprint="0" * 64,
        result={"component_ids": {}, "plan": [], "warnings": []},
    )
    bundle_repository = api.evidence_bundles.repository.evidence_bundles
    lookups = 0

    def winner_after_race(**kwargs):  # noqa: ANN003
        nonlocal lookups
        lookups += 1
        return None if lookups == 1 else winner

    monkeypatch.setattr(bundle_repository, "get_by_key", winner_after_race)
    monkeypatch.setattr(
        bundle_repository,
        "insert",
        lambda _record: (_ for _ in ()).throw(EvidenceBundleKeyRaceError("race")),
    )

    with pytest.raises(ConflictError, match="conflicting fields"):
        api.record_evidence_bundle(command, actor=actor)

    assert api.list_notes(project_id=project.project_id) == []


def test_invalid_late_component_is_rejected_before_any_component_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = _actor()
    project, question = _project_and_question(api, actor)
    writes = 0
    original_save = api.evidence_bundles.repository.datasets.save

    def count_save(entity):  # noqa: ANN001
        nonlocal writes
        writes += 1
        return original_save(entity)

    monkeypatch.setattr(api.evidence_bundles.repository.datasets, "save", count_save)
    command = RecordEvidenceBundleCommand(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        dataset=CreateDatasetIntent(),
        claim=CreateClaimIntent(
            statement="Invalid rejected claim",
            confidence=50,
            status="rejected",
        ),
        dry_run=False,
        idempotency_key="prepare-before-write",
    )

    with pytest.raises(ValidationError, match="terminal_reason"):
        api.record_evidence_bundle(command, actor=actor)

    assert writes == 0


@pytest.mark.parametrize("dry_run", [True, False])
def test_direct_bundle_rejects_supported_claim_without_evidence(
    dry_run: bool,
) -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Unsupported direct bundle", actor=actor)
    command = RecordEvidenceBundleCommand(
        project_id=project.project_id,
        claim=CreateClaimIntent(
            statement="Unbacked bundle claim",
            confidence=80,
            status=ClaimStatus.SUPPORTED,
        ),
        dry_run=dry_run,
        idempotency_key=None if dry_run else "unsupported-direct-bundle",
    )

    with pytest.raises(ValidationError, match="Supported claims require"):
        api.record_evidence_bundle(command, actor=actor)

    assert api.list_claims(project_id=project.project_id) == []
    assert api.evidence_bundles.repository.evidence_bundles.list() == []


def test_direct_bundle_rejects_reserved_metadata_key() -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Direct reserved key", actor=actor)
    command = RecordEvidenceBundleCommand(
        project_id=project.project_id,
        source_note=CreateSourceNoteIntent(
            raw_content="Do not persist the key",
            metadata=(("lab_tracker_evidence_bundle_idempotency_key", "secret"),),
        ),
        dry_run=False,
        idempotency_key="secret",
    )

    with pytest.raises(ValidationError, match="reserved"):
        api.record_evidence_bundle(command, actor=actor)

    assert api.list_notes(project_id=project.project_id) == []


@pytest.mark.parametrize(
    "key,upload",
    [
        ("x" * 201, None),
        (
            "valid-key",
            EvidenceBundleUploadIntent(
                checksum_sha256="not-a-checksum",
                size_bytes=1,
                filename="figure.png",
                content_type="image/png",
            ),
        ),
        (
            "valid-key",
            EvidenceBundleUploadIntent(
                checksum_sha256="a" * 64,
                size_bytes=0,
                filename="figure.png",
                content_type="image/png",
            ),
        ),
        (
            "valid-key",
            EvidenceBundleUploadIntent(
                checksum_sha256="a" * 64,
                size_bytes=1,
                filename="figure.svg",
                content_type="image/svg+xml",
            ),
        ),
    ],
)
def test_direct_bundle_enforces_key_and_upload_intent_bounds(
    key: str,
    upload: EvidenceBundleUploadIntent | None,
) -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Direct boundary validation", actor=actor)
    command = RecordEvidenceBundleCommand(
        project_id=project.project_id,
        source_note=(
            CreateSourceNoteIntent(raw_content="Boundary check") if upload is None else None
        ),
        visualization=(
            ExistingVisualizationIntent(
                visualization_id=uuid4(),
                upload_intent=upload,
            )
            if upload is not None
            else None
        ),
        dry_run=False,
        idempotency_key=key,
    )

    with pytest.raises(ValidationError):
        api.record_evidence_bundle(command, actor=actor)


def test_direct_bundle_rejects_an_ambient_application_transaction() -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Outermost bundle boundary", actor=actor)
    command = RecordEvidenceBundleCommand(
        project_id=project.project_id,
        source_note=CreateSourceNoteIntent(raw_content="Must own the boundary"),
        dry_run=False,
        idempotency_key="outermost-only",
    )

    with (
        api.projects.application_transaction(),
        pytest.raises(RuntimeError, match="outer application transaction"),
    ):
        api.record_evidence_bundle(command, actor=actor)

    assert api.list_notes(project_id=project.project_id) == []


def test_keyed_dry_run_reuses_or_conflicts_without_mutating() -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Keyed dry-run parity", actor=actor)
    committed = RecordEvidenceBundleCommand(
        project_id=project.project_id,
        source_note=CreateSourceNoteIntent(raw_content="Canonical evidence"),
        dry_run=False,
        idempotency_key="keyed-preview",
    )
    created = api.record_evidence_bundle(committed, actor=actor)
    note_count = len(api.list_notes(project_id=project.project_id))
    record_count = len(api.evidence_bundles.repository.evidence_bundles.list())

    reused = api.record_evidence_bundle(replace(committed, dry_run=True), actor=actor)
    with pytest.raises(ConflictError, match="conflicting fields"):
        api.record_evidence_bundle(
            replace(
                committed,
                source_note=CreateSourceNoteIntent(raw_content="Different evidence"),
                dry_run=True,
            ),
            actor=actor,
        )

    assert reused.outcome == "reused"
    assert reused.component_ids == created.component_ids
    assert len(api.list_notes(project_id=project.project_id)) == note_count
    assert len(api.evidence_bundles.repository.evidence_bundles.list()) == record_count


def test_non_admin_cannot_distinguish_foreign_and_missing_component_ids() -> None:
    api = repository_backed_api()
    admin = _actor()
    visible_project = api.create_project("Visible bundle project", actor=admin)
    hidden_project = api.create_project("Hidden bundle project", actor=admin)
    hidden_note = api.create_note(
        hidden_project.project_id,
        "Hidden source note",
        actor=admin,
    )
    contributor = AuthContext(user_id=uuid4(), role=Role.VIEWER)
    _, session = api._test_resources  # type: ignore[attr-defined]
    session.add(
        UserModel(
            user_id=str(contributor.user_id),
            username=f"bundle-contributor-{contributor.user_id.hex}",
            password_hash="unused",
            role=Role.VIEWER.value,
        )
    )
    session.commit()
    api.upsert_project_membership(
        visible_project.project_id,
        contributor.user_id,
        ProjectMembershipRole.CONTRIBUTOR,
        actor=admin,
    )

    messages: list[str] = []
    for note_id in (hidden_note.note_id, uuid4()):
        with pytest.raises(NotFoundError) as exc_info:
            api.record_evidence_bundle(
                RecordEvidenceBundleCommand(
                    project_id=visible_project.project_id,
                    source_note=ExistingSourceNoteIntent(note_id=note_id),
                    dry_run=True,
                ),
                actor=contributor,
            )
        messages.append(str(exc_info.value))

    assert messages == [
        "Source note does not exist in the bundle project.",
        "Source note does not exist in the bundle project.",
    ]
