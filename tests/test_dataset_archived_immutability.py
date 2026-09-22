"""Archiving a dataset must not reopen its committed provenance (M54)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from api_helpers import repository_backed_api

from lab_tracker.auth import AuthContext, Role
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    DatasetCommitManifestInput,
    DatasetFile,
    DatasetStatus,
    QuestionLink,
    QuestionLinkRole,
    QuestionStatus,
    QuestionType,
)


def _actor() -> AuthContext:
    return AuthContext(user_id=uuid4(), role=Role.ADMIN)


def _archived_dataset(*, commit_first: bool):
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Archived dataset provenance", actor=actor)
    questions = [
        api.create_question(
            project_id=project.project_id,
            text=text,
            question_type=QuestionType.DESCRIPTIVE,
            status=QuestionStatus.ACTIVE,
            actor=actor,
        )
        for text in ("Original primary question?", "Replacement primary question?")
    ]
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=questions[0].question_id,
        commit_manifest=DatasetCommitManifestInput(
            files=[DatasetFile(path="data.csv", checksum="abc123")]
        ),
        status=DatasetStatus.COMMITTED if commit_first else DatasetStatus.STAGED,
        actor=actor,
    )
    archived = api.update_dataset(
        dataset.dataset_id,
        status=DatasetStatus.ARCHIVED,
        terminal_reason="Superseded by a corrected acquisition.",
        actor=actor,
    )
    assert archived.status == DatasetStatus.ARCHIVED
    return api, actor, archived, questions[1]


@pytest.mark.parametrize("commit_first", [True, False], ids=["committed", "staged"])
@pytest.mark.parametrize("field", ["commit_manifest", "question_links", "commit_hash"])
def test_archived_dataset_rejects_provenance_rewrites(commit_first: bool, field: str) -> None:
    api, actor, archived, replacement_question = _archived_dataset(commit_first=commit_first)
    patches = {
        "commit_manifest": DatasetCommitManifestInput(
            files=[DatasetFile(path="data.csv", checksum="zzz")]
        ),
        "question_links": [
            QuestionLink(
                question_id=replacement_question.question_id,
                role=QuestionLinkRole.PRIMARY,
            )
        ],
        "commit_hash": archived.commit_hash,
    }

    with pytest.raises(ValidationError, match="^Archived datasets are immutable\\.$"):
        api.update_dataset(archived.dataset_id, actor=actor, **{field: patches[field]})

    reloaded = api.get_dataset(archived.dataset_id)
    assert reloaded.commit_hash == archived.commit_hash
    assert reloaded.commit_manifest == archived.commit_manifest
    assert reloaded.question_links == archived.question_links
    assert reloaded.primary_question_id == archived.primary_question_id


def test_archived_dataset_still_allows_terminal_reason_edits() -> None:
    api, actor, archived, _ = _archived_dataset(commit_first=True)

    updated = api.update_dataset(
        archived.dataset_id,
        terminal_reason="Raw acquisition was corrupted.",
        actor=actor,
    )

    assert updated.status == DatasetStatus.ARCHIVED
    assert updated.terminal_reason == "Raw acquisition was corrupted."
    assert updated.commit_hash == archived.commit_hash
    assert updated.commit_manifest == archived.commit_manifest
