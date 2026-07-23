"""Claim confidence and application-layer support invariants."""

from __future__ import annotations

from datetime import datetime, timezone
from math import inf, nan
from uuid import uuid4

import pytest
from api_helpers import repository_backed_api
from pydantic import ValidationError as PydanticValidationError

from lab_tracker.auth import AuthContext, Role
from lab_tracker.db_models import ClaimModel
from lab_tracker.errors import ValidationError
from lab_tracker.models import Claim, ClaimInput, ClaimStatus
from lab_tracker.schemas import ClaimCreate, ClaimUpdate, EvidenceBundleCreateClaim
from lab_tracker.sqlalchemy_mappers import claim_from_model


def _actor() -> AuthContext:
    return AuthContext(user_id=uuid4(), role=Role.ADMIN)


def _claim_kwargs() -> dict[str, object]:
    return {
        "claim_id": uuid4(),
        "project_id": uuid4(),
        "statement": "The response is stable.",
    }


@pytest.mark.parametrize("confidence", [-1.0, 100.1, nan, inf, -inf])
@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (ClaimInput, {"statement": "Bounded input"}),
        (Claim, _claim_kwargs()),
    ],
)
def test_claim_domain_models_reject_invalid_confidence(
    model,
    kwargs: dict[str, object],
    confidence: float,
) -> None:
    with pytest.raises(PydanticValidationError):
        model(**kwargs, confidence=confidence)


@pytest.mark.parametrize("confidence", [0.0, 100.0])
def test_claim_domain_accepts_inclusive_confidence_bounds(confidence: float) -> None:
    assert Claim(**_claim_kwargs(), confidence=confidence).confidence == confidence
    assert ClaimInput(statement="Boundary input", confidence=confidence).confidence == confidence


@pytest.mark.parametrize("confidence", [-1.0, 100.1, nan, inf, -inf])
def test_claim_assignment_rejects_invalid_confidence_without_mutating_state(
    confidence: float,
) -> None:
    claim = Claim(**_claim_kwargs(), confidence=50.0)

    with pytest.raises(PydanticValidationError):
        claim.confidence = confidence

    assert claim.confidence == 50.0


@pytest.mark.parametrize(
    "schema",
    [
        lambda confidence: ClaimCreate(
            project_id=uuid4(),
            statement="Create request",
            confidence=confidence,
        ),
        lambda confidence: ClaimUpdate(confidence=confidence),
        lambda confidence: EvidenceBundleCreateClaim(
            kind="create",
            statement="Bundle request",
            confidence=confidence,
        ),
    ],
)
@pytest.mark.parametrize("confidence", [-1.0, 100.1, nan, inf, -inf])
def test_claim_request_models_share_the_finite_confidence_bound(
    schema,
    confidence: float,
) -> None:
    with pytest.raises(PydanticValidationError):
        schema(confidence)


@pytest.mark.parametrize("confidence", [-1.0, 100.1, nan, inf, -inf])
def test_direct_claim_creation_rejects_invalid_confidence_before_writing(
    confidence: float,
) -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Bounded confidence", actor=actor)

    with pytest.raises(
        ValidationError,
        match="finite value between 0 and 100",
    ):
        api.create_claim(
            project.project_id,
            "Invalid confidence",
            confidence,
            actor=actor,
        )

    assert api.list_claims(project_id=project.project_id) == []


@pytest.mark.parametrize("confidence", [-1.0, 100.1, nan, inf, -inf])
def test_direct_claim_update_rejects_invalid_confidence_without_mutating_record(
    confidence: float,
) -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Stable confidence", actor=actor)
    claim = api.create_claim(
        project.project_id,
        "Original statement",
        50.0,
        actor=actor,
    )

    with pytest.raises(
        ValidationError,
        match="finite value between 0 and 100",
    ):
        api.update_claim(
            claim.claim_id,
            statement="Must not leak through",
            confidence=confidence,
            actor=actor,
        )

    reloaded = api.get_claim(claim.claim_id)
    assert reloaded.statement == "Original statement"
    assert reloaded.confidence == 50.0


def test_direct_supported_claim_writes_require_evidence_and_preserve_state() -> None:
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Supported claims", actor=actor)

    with pytest.raises(ValidationError, match="Supported claims require"):
        api.create_claim(
            project.project_id,
            "Unbacked at creation",
            80.0,
            status=ClaimStatus.SUPPORTED,
            actor=actor,
        )

    proposed = api.create_claim(
        project.project_id,
        "Still proposed",
        40.0,
        actor=actor,
    )
    with pytest.raises(ValidationError, match="Supported claims require"):
        api.update_claim(
            proposed.claim_id,
            statement="Must remain unchanged",
            confidence=90.0,
            status=ClaimStatus.SUPPORTED,
            actor=actor,
        )

    reloaded = api.get_claim(proposed.claim_id)
    assert reloaded.statement == "Still proposed"
    assert reloaded.confidence == 40.0
    assert reloaded.status == ClaimStatus.PROPOSED


def test_claim_mapper_rejects_corrupt_confidence_but_reads_legacy_missing_support() -> None:
    timestamp = datetime(2026, 7, 22, tzinfo=timezone.utc)
    row = ClaimModel(
        claim_id=uuid4(),
        project_id=uuid4(),
        statement="Legacy supported claim",
        confidence=80.0,
        status="supported",
        created_at=timestamp,
        updated_at=timestamp,
    )

    legacy = claim_from_model(row)
    assert legacy.status == ClaimStatus.SUPPORTED
    assert legacy.supported_by_dataset_ids == []
    assert legacy.supported_by_analysis_ids == []

    row.confidence = 101.0
    with pytest.raises(PydanticValidationError):
        claim_from_model(row)
