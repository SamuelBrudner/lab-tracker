"""Atomic evidence-bundle command route."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from starlette import status as http_status
from starlette.requests import Request
from starlette.responses import JSONResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.schemas import (
    Envelope,
    EvidenceBundleCreateAnalysis,
    EvidenceBundleCreateClaim,
    EvidenceBundleCreateDataset,
    EvidenceBundleCreateSourceNote,
    EvidenceBundleCreateVisualization,
    EvidenceBundleExistingAnalysis,
    EvidenceBundleExistingClaim,
    EvidenceBundleExistingDataset,
    EvidenceBundleExistingSourceNote,
    EvidenceBundleExistingVisualization,
    EvidenceBundleRequest,
    EvidenceBundleResultRead,
)
from lab_tracker.services.evidence_bundle_service import (
    CreateAnalysisIntent,
    CreateClaimIntent,
    CreateDatasetIntent,
    CreateSourceNoteIntent,
    CreateVisualizationIntent,
    EvidenceBundleResult,
    EvidenceBundleUploadIntent,
    ExistingAnalysisIntent,
    ExistingClaimIntent,
    ExistingDatasetIntent,
    ExistingSourceNoteIntent,
    ExistingVisualizationIntent,
    RecordEvidenceBundleCommand,
)

from .shared import actor_from_request, api_from_request


def build_evidence_bundles_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/evidence-bundles",
        response_model=Envelope[EvidenceBundleResultRead],
        responses={
            http_status.HTTP_201_CREATED: {
                "model": Envelope[EvidenceBundleResultRead],
                "description": "A new evidence bundle was recorded atomically.",
            }
        },
    )
    def record_evidence_bundle(payload: EvidenceBundleRequest, request: Request):
        actor = actor_from_request(request)
        result = api_from_request(request, api).record_evidence_bundle(
            _command_from_request(payload),
            actor=actor,
        )
        envelope = Envelope(data=_result_read(result))
        if result.outcome == "created":
            return JSONResponse(
                status_code=http_status.HTTP_201_CREATED,
                content=jsonable_encoder(envelope),
            )
        return envelope

    return router


def _command_from_request(payload: EvidenceBundleRequest) -> RecordEvidenceBundleCommand:
    return RecordEvidenceBundleCommand(
        project_id=payload.project_id,
        primary_question_id=payload.primary_question_id,
        dataset=_dataset_intent(payload.dataset),
        analysis=_analysis_intent(payload.analysis),
        claim=_claim_intent(payload.claim),
        visualization=_visualization_intent(payload.visualization),
        source_note=_source_note_intent(payload.source_note),
        dry_run=payload.dry_run,
        idempotency_key=payload.idempotency_key,
    )


def _dataset_intent(payload):
    if payload is None:
        return None
    if isinstance(payload, EvidenceBundleExistingDataset):
        return ExistingDatasetIntent(dataset_id=payload.dataset_id)
    if isinstance(payload, EvidenceBundleCreateDataset):
        return CreateDatasetIntent(
            primary_question_id=payload.primary_question_id,
            secondary_question_ids=tuple(payload.secondary_question_ids or ()),
            commit_manifest=payload.commit_manifest,
            commit_hash=payload.commit_hash,
            status=payload.status,
            terminal_reason=payload.terminal_reason,
        )
    raise TypeError("Unsupported evidence-bundle dataset intent.")


def _analysis_intent(payload):
    if payload is None:
        return None
    if isinstance(payload, EvidenceBundleExistingAnalysis):
        return ExistingAnalysisIntent(analysis_id=payload.analysis_id)
    if isinstance(payload, EvidenceBundleCreateAnalysis):
        return CreateAnalysisIntent(
            dataset_ids=tuple(payload.dataset_ids or ()),
            method_hash=payload.method_hash,
            code_version=payload.code_version,
            environment_hash=payload.environment_hash,
            external_artifacts=tuple(payload.external_artifacts or ()),
            status=payload.status,
            terminal_reason=payload.terminal_reason,
            derive_code_provenance=payload.derive_code_provenance,
        )
    raise TypeError("Unsupported evidence-bundle analysis intent.")


def _claim_intent(payload):
    if payload is None:
        return None
    if isinstance(payload, EvidenceBundleExistingClaim):
        return ExistingClaimIntent(claim_id=payload.claim_id)
    if isinstance(payload, EvidenceBundleCreateClaim):
        return CreateClaimIntent(
            statement=payload.statement,
            confidence=payload.confidence,
            status=payload.status,
            terminal_reason=payload.terminal_reason,
            falsification_criteria=payload.falsification_criteria,
            verification_plan=payload.verification_plan,
            refuting_outcome=payload.refuting_outcome,
            supported_by_dataset_ids=tuple(payload.supported_by_dataset_ids or ()),
            supported_by_analysis_ids=tuple(payload.supported_by_analysis_ids or ()),
            answers_question_ids=tuple(payload.answers_question_ids or ()),
            external_citations=tuple(payload.external_citations or ()),
        )
    raise TypeError("Unsupported evidence-bundle claim intent.")


def _visualization_intent(payload):
    if payload is None:
        return None
    upload_intent = _upload_intent(payload.upload_intent)
    if isinstance(payload, EvidenceBundleExistingVisualization):
        return ExistingVisualizationIntent(
            visualization_id=payload.viz_id,
            upload_intent=upload_intent,
        )
    if isinstance(payload, EvidenceBundleCreateVisualization):
        return CreateVisualizationIntent(
            analysis_id=payload.analysis_id,
            viz_type=payload.viz_type,
            file_path=payload.file_path,
            caption=payload.caption,
            related_claim_ids=tuple(payload.related_claim_ids or ()),
            upload_intent=upload_intent,
        )
    raise TypeError("Unsupported evidence-bundle visualization intent.")


def _source_note_intent(payload):
    if payload is None:
        return None
    if isinstance(payload, EvidenceBundleExistingSourceNote):
        return ExistingSourceNoteIntent(note_id=payload.note_id)
    if isinstance(payload, EvidenceBundleCreateSourceNote):
        return CreateSourceNoteIntent(
            raw_content=payload.raw_content,
            transcribed_text=payload.transcribed_text,
            targets=tuple(payload.targets) if payload.targets is not None else None,
            metadata=tuple(sorted((payload.metadata or {}).items())),
            status=payload.status,
        )
    raise TypeError("Unsupported evidence-bundle source-note intent.")


def _upload_intent(payload) -> EvidenceBundleUploadIntent | None:
    if payload is None:
        return None
    return EvidenceBundleUploadIntent(
        checksum_sha256=payload.checksum_sha256,
        size_bytes=payload.size_bytes,
        filename=payload.filename,
        content_type=payload.content_type,
    )


def _result_read(result: EvidenceBundleResult) -> EvidenceBundleResultRead:
    return EvidenceBundleResultRead.model_validate(result.as_json())
