"""Atomic, server-enforced evidence-bundle application command."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Literal, TypeAlias
from uuid import UUID, uuid4

from pydantic import BaseModel

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ConflictError, NotFoundError, ValidationError
from lab_tracker.models import (
    AnalysisStatus,
    ClaimStatus,
    DatasetCommitManifest,
    DatasetCommitManifestInput,
    DatasetStatus,
    EntityRef,
    EntityType,
    EvidenceBundleRecord,
    ExternalArtifactReference,
    NoteStatus,
    QuestionLink,
    QuestionLinkRole,
    external_artifact_uri_validation_error,
)
from lab_tracker.repository import EvidenceBundleKeyRaceError
from lab_tracker.services.analysis_service import AnalysisService
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.claim_service import ClaimService
from lab_tracker.services.dataset_service import DatasetService
from lab_tracker.services.note_service import NoteService
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.question_service import QuestionService
from lab_tracker.services.shared import (
    actor_user_id,
    build_commit_manifest,
    compute_commit_hash,
    dataset_manifest_payload,
    ensure_primary_question_active,
    normalize_note_metadata,
    terminal_reason_for_status,
    unique_ids,
    validate_commit_hash,
)
from lab_tracker.services.visualization_service import VisualizationService
from lab_tracker.upload_security import validate_upload_content_type

EVIDENCE_BUNDLE_IDEMPOTENCY_METADATA_KEY = "lab_tracker_evidence_bundle_idempotency_key"
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class EvidenceBundleUploadIntent:
    checksum_sha256: str
    size_bytes: int
    filename: str
    content_type: str


@dataclass(frozen=True)
class ExistingDatasetIntent:
    dataset_id: UUID


@dataclass(frozen=True)
class CreateDatasetIntent:
    primary_question_id: UUID | None = None
    secondary_question_ids: tuple[UUID, ...] = ()
    commit_manifest: DatasetCommitManifestInput | DatasetCommitManifest | None = None
    commit_hash: str | None = None
    status: DatasetStatus = DatasetStatus.STAGED
    terminal_reason: str | None = None
    manifest_file_sizes: tuple[tuple[str, int | None], ...] = ()


DatasetIntent: TypeAlias = ExistingDatasetIntent | CreateDatasetIntent


@dataclass(frozen=True)
class ExistingAnalysisIntent:
    analysis_id: UUID


@dataclass(frozen=True)
class CreateAnalysisIntent:
    dataset_ids: tuple[UUID, ...]
    method_hash: str
    code_version: str
    environment_hash: str | None = None
    external_artifacts: tuple[ExternalArtifactReference, ...] = ()
    status: AnalysisStatus = AnalysisStatus.STAGED
    terminal_reason: str | None = None
    derive_code_provenance: bool = False


AnalysisIntent: TypeAlias = ExistingAnalysisIntent | CreateAnalysisIntent


@dataclass(frozen=True)
class ExistingClaimIntent:
    claim_id: UUID


@dataclass(frozen=True)
class CreateClaimIntent:
    statement: str
    confidence: float
    status: ClaimStatus = ClaimStatus.PROPOSED
    terminal_reason: str | None = None
    falsification_criteria: str | None = None
    verification_plan: str | None = None
    refuting_outcome: str | None = None
    supported_by_dataset_ids: tuple[UUID, ...] = ()
    supported_by_analysis_ids: tuple[UUID, ...] = ()
    answers_question_ids: tuple[UUID, ...] = ()
    external_citations: tuple[ExternalArtifactReference, ...] = ()


ClaimIntent: TypeAlias = ExistingClaimIntent | CreateClaimIntent


@dataclass(frozen=True)
class ExistingVisualizationIntent:
    visualization_id: UUID
    upload_intent: EvidenceBundleUploadIntent | None = None


@dataclass(frozen=True)
class CreateVisualizationIntent:
    analysis_id: UUID | None
    viz_type: str
    file_path: str
    caption: str | None = None
    related_claim_ids: tuple[UUID, ...] = ()
    upload_intent: EvidenceBundleUploadIntent | None = None


VisualizationIntent: TypeAlias = ExistingVisualizationIntent | CreateVisualizationIntent


@dataclass(frozen=True)
class ExistingSourceNoteIntent:
    note_id: UUID


@dataclass(frozen=True)
class CreateSourceNoteIntent:
    raw_content: str
    transcribed_text: str | None = None
    targets: tuple[EntityRef, ...] | None = None
    metadata: tuple[tuple[str, str], ...] = ()
    status: NoteStatus = NoteStatus.STAGED


SourceNoteIntent: TypeAlias = ExistingSourceNoteIntent | CreateSourceNoteIntent


@dataclass(frozen=True)
class RecordEvidenceBundleCommand:
    project_id: UUID
    primary_question_id: UUID | None = None
    dataset: DatasetIntent | None = None
    analysis: AnalysisIntent | None = None
    claim: ClaimIntent | None = None
    visualization: VisualizationIntent | None = None
    source_note: SourceNoteIntent | None = None
    dry_run: bool = True
    idempotency_key: str | None = None


@dataclass(frozen=True)
class EvidenceBundleComponentIds:
    dataset_id: UUID | None = None
    analysis_id: UUID | None = None
    claim_id: UUID | None = None
    visualization_id: UUID | None = None
    source_note_id: UUID | None = None

    def as_json(self) -> dict[str, str | None]:
        return {
            "dataset_id": _uuid_text(self.dataset_id),
            "analysis_id": _uuid_text(self.analysis_id),
            "claim_id": _uuid_text(self.claim_id),
            "visualization_id": _uuid_text(self.visualization_id),
            "source_note_id": _uuid_text(self.source_note_id),
        }


@dataclass(frozen=True)
class EvidenceBundlePlanStep:
    action: Literal["create", "reuse"]
    entity_type: Literal["dataset", "analysis", "claim", "visualization", "source_note"]
    entity_id: UUID | None = None
    reason: str | None = None
    details: dict[str, Any] | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": _uuid_text(self.entity_id),
            "reason": self.reason,
            "details": _jsonable(self.details),
        }


@dataclass(frozen=True)
class EvidenceBundleResult:
    outcome: Literal["preview", "created", "reused"]
    project_id: UUID
    idempotency_key: str | None
    component_ids: EvidenceBundleComponentIds
    plan: tuple[EvidenceBundlePlanStep, ...]
    warnings: tuple[str, ...] = ()

    @property
    def dry_run(self) -> bool:
        return self.outcome == "preview"

    def as_json(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "dry_run": self.dry_run,
            "project_id": str(self.project_id),
            "idempotency_key": self.idempotency_key,
            "component_ids": self.component_ids.as_json(),
            "plan": [step.as_json() for step in self.plan],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_record(cls, record: EvidenceBundleRecord) -> EvidenceBundleResult:
        payload = record.result
        ids = payload.get("component_ids") or {}
        stored_plan = payload.get("plan") or []
        return cls(
            outcome="reused",
            project_id=record.project_id,
            idempotency_key=record.idempotency_key,
            component_ids=EvidenceBundleComponentIds(
                dataset_id=_optional_uuid(ids.get("dataset_id")),
                analysis_id=_optional_uuid(ids.get("analysis_id")),
                claim_id=_optional_uuid(ids.get("claim_id")),
                visualization_id=_optional_uuid(ids.get("visualization_id")),
                source_note_id=_optional_uuid(ids.get("source_note_id")),
            ),
            plan=tuple(
                EvidenceBundlePlanStep(
                    action="reuse",
                    entity_type=step["entity_type"],
                    entity_id=_optional_uuid(step.get("entity_id")),
                    reason="idempotent_replay",
                    details=(
                        dict(step["details"]) if isinstance(step.get("details"), dict) else None
                    ),
                )
                for step in stored_plan
            ),
            warnings=tuple(str(item) for item in payload.get("warnings") or []),
        )


@dataclass(frozen=True)
class _PreparedEvidenceBundle:
    command: RecordEvidenceBundleCommand
    request_fingerprint: str


class EvidenceBundleService(BaseService):
    """Prepare and record one evidence graph as an atomic application command."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        questions: QuestionService,
        datasets: DatasetService,
        analyses: AnalysisService,
        claims: ClaimService,
        visualizations: VisualizationService,
        notes: NoteService,
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.questions = questions
        self.datasets = datasets
        self.analyses = analyses
        self.claims = claims
        self.visualizations = visualizations
        self.notes = notes
        self.authorization = authorization

    def record(
        self,
        command: RecordEvidenceBundleCommand,
        *,
        actor: AuthContext,
    ) -> EvidenceBundleResult:
        """Preview or atomically persist an evidence bundle.

        The final idempotency-row flush is deliberately inside the application
        boundary. A concurrent loser exits that boundary, rolls back every
        speculative entity, and only then reloads the committed winner.
        """

        self._validate_command_boundary(command)
        created_by = _required_actor_id(actor)
        if not self._context.is_request_managed() and self._context.transaction.active:
            raise RuntimeError(
                "Evidence-bundle recording must own the outer application transaction."
            )
        # Authorization must precede idempotency lookup: replayed component IDs
        # are project data and must not remain visible after access is revoked.
        self.authorization.require_contributor(command.project_id, actor=actor)
        self.projects.get_project(command.project_id)
        normalized_command = _normalize_command(command)
        request_fingerprint = self._fingerprint(normalized_command)

        if normalized_command.dry_run:
            with self.application_transaction():
                idempotency_key = _clean_optional(normalized_command.idempotency_key)
                if idempotency_key is not None:
                    existing = self.repository.evidence_bundles.get_by_key(
                        project_id=normalized_command.project_id,
                        created_by=created_by,
                        idempotency_key=idempotency_key,
                    )
                    if existing is not None:
                        return self._compare_replay(existing, request_fingerprint)
                prepared = self.prepare(
                    normalized_command,
                    actor=actor,
                    request_fingerprint=request_fingerprint,
                )
                return self._preview(prepared)

        try:
            with self.application_transaction():
                existing = self.repository.evidence_bundles.get_by_key(
                    project_id=normalized_command.project_id,
                    created_by=created_by,
                    idempotency_key=_required_key(normalized_command.idempotency_key),
                )
                if existing is not None:
                    return self._compare_replay(existing, request_fingerprint)
                prepared = self.prepare(
                    normalized_command,
                    actor=actor,
                    request_fingerprint=request_fingerprint,
                )
                result = self._execute(prepared, actor=actor)
                self.repository.evidence_bundles.insert(
                    EvidenceBundleRecord(
                        bundle_id=uuid4(),
                        project_id=normalized_command.project_id,
                        created_by=created_by,
                        idempotency_key=_required_key(normalized_command.idempotency_key),
                        request_fingerprint=request_fingerprint,
                        result=result.as_json(),
                    )
                )
                return result
        except EvidenceBundleKeyRaceError as exc:
            # Request-managed boundaries are otherwise owned by middleware;
            # direct boundaries have already rolled back. An explicit full
            # rollback is safe in both cases and is essential: a savepoint here
            # would retain the loser's speculative graph.
            self.repository.rollback()
            with self.application_transaction():
                winner = self.repository.evidence_bundles.get_by_key(
                    project_id=normalized_command.project_id,
                    created_by=created_by,
                    idempotency_key=_required_key(normalized_command.idempotency_key),
                )
                if winner is None:
                    raise exc
                return self._compare_replay(winner, request_fingerprint)

    def prepare(
        self,
        command: RecordEvidenceBundleCommand,
        *,
        actor: AuthContext,
        request_fingerprint: str | None = None,
    ) -> _PreparedEvidenceBundle:
        """Resolve and validate the complete command without writing."""

        self.authorization.require_contributor(command.project_id, actor=actor)
        self.projects.get_project(command.project_id)
        normalized_command = _normalize_command(command)
        self._validate_primary_question(normalized_command)
        self._validate_dataset(normalized_command)
        self._validate_analysis(normalized_command)
        self._validate_claim(normalized_command)
        self._validate_visualization(normalized_command)
        self._validate_source_note(normalized_command)
        return _PreparedEvidenceBundle(
            command=normalized_command,
            request_fingerprint=(request_fingerprint or self._fingerprint(normalized_command)),
        )

    def _validate_command_boundary(self, command: RecordEvidenceBundleCommand) -> None:
        if not any(
            component is not None
            for component in (
                command.dataset,
                command.analysis,
                command.claim,
                command.visualization,
                command.source_note,
            )
        ):
            raise ValidationError("At least one evidence-bundle component is required.")
        if not command.dry_run:
            _required_key(command.idempotency_key)

    def _validate_primary_question(self, command: RecordEvidenceBundleCommand) -> None:
        if command.primary_question_id is None:
            return
        self._project_component(
            entity_id=command.primary_question_id,
            project_id=command.project_id,
            label="Primary question",
            loader=self.questions.get_question,
        )

    def _validate_dataset(self, command: RecordEvidenceBundleCommand) -> None:
        intent = command.dataset
        if intent is None:
            return
        if isinstance(intent, ExistingDatasetIntent):
            self._project_component(
                entity_id=intent.dataset_id,
                project_id=command.project_id,
                label="Dataset",
                loader=self.datasets.get_dataset,
            )
            return
        primary_question_id = intent.primary_question_id or command.primary_question_id
        if primary_question_id is None:
            raise ValidationError("Dataset create requires primary_question_id.")
        primary = self._project_component(
            entity_id=primary_question_id,
            project_id=command.project_id,
            label="Primary question",
            loader=self.questions.get_question,
        )
        for question_id in intent.secondary_question_ids:
            self._project_component(
                entity_id=question_id,
                project_id=command.project_id,
                label="Secondary question",
                loader=self.questions.get_question,
            )
        manifest = intent.commit_manifest
        if not isinstance(manifest, DatasetCommitManifest):
            raise RuntimeError("Evidence-bundle dataset intent was not normalized.")
        if manifest.source_session_id is not None:
            self._project_component(
                entity_id=manifest.source_session_id,
                project_id=command.project_id,
                label="Source session",
                loader=self.datasets.sessions.get_session,
            )
            self.datasets.validate_source_session(
                manifest.source_session_id,
                command.project_id,
            )
        for note_id in manifest.note_ids:
            self._project_component(
                entity_id=note_id,
                project_id=command.project_id,
                label="Dataset manifest note",
                loader=self.notes.get_note,
            )
        if intent.status == DatasetStatus.COMMITTED:
            if not manifest.files and not manifest.external_artifacts:
                raise ValidationError(
                    "At least one file or external artifact is required to commit a dataset."
                )
            ensure_primary_question_active(primary)

    def _validate_analysis(self, command: RecordEvidenceBundleCommand) -> None:
        intent = command.analysis
        if intent is None:
            return
        if isinstance(intent, ExistingAnalysisIntent):
            self._project_component(
                entity_id=intent.analysis_id,
                project_id=command.project_id,
                label="Analysis",
                loader=self.analyses.get_analysis,
            )
            return
        datasets = [
            self._project_component(
                entity_id=dataset_id,
                project_id=command.project_id,
                label="Analysis dataset",
                loader=self.datasets.get_dataset,
            )
            for dataset_id in intent.dataset_ids
        ]
        bundle_dataset_status: DatasetStatus | None = None
        if isinstance(command.dataset, ExistingDatasetIntent):
            bundle_dataset = self._project_component(
                entity_id=command.dataset.dataset_id,
                project_id=command.project_id,
                label="Dataset",
                loader=self.datasets.get_dataset,
            )
            bundle_dataset_status = bundle_dataset.status
        elif isinstance(command.dataset, CreateDatasetIntent):
            bundle_dataset_status = command.dataset.status
        if not datasets and command.dataset is None:
            raise ValidationError("Analysis create requires a dataset reference.")
        if intent.status == AnalysisStatus.COMMITTED:
            if any(dataset.status != DatasetStatus.COMMITTED for dataset in datasets):
                raise ValidationError("Committed analysis requires committed datasets.")
            if bundle_dataset_status not in {None, DatasetStatus.COMMITTED}:
                raise ValidationError("Committed analysis requires a committed bundle dataset.")

    def _validate_claim(self, command: RecordEvidenceBundleCommand) -> None:
        intent = command.claim
        if intent is None:
            return
        if isinstance(intent, ExistingClaimIntent):
            self._project_component(
                entity_id=intent.claim_id,
                project_id=command.project_id,
                label="Claim",
                loader=self.claims.get_claim,
            )
            return
        if not math.isfinite(intent.confidence) or not 0 <= intent.confidence <= 100:
            raise ValidationError("confidence must be a finite value between 0 and 100.")
        for dataset_id in intent.supported_by_dataset_ids:
            self._project_component(
                entity_id=dataset_id,
                project_id=command.project_id,
                label="Supporting dataset",
                loader=self.datasets.get_dataset,
            )
        for analysis_id in intent.supported_by_analysis_ids:
            self._project_component(
                entity_id=analysis_id,
                project_id=command.project_id,
                label="Supporting analysis",
                loader=self.analyses.get_analysis,
            )
        for question_id in intent.answers_question_ids:
            self._project_component(
                entity_id=question_id,
                project_id=command.project_id,
                label="Answered question",
                loader=self.questions.get_question,
            )
        has_support = bool(
            intent.supported_by_dataset_ids
            or intent.supported_by_analysis_ids
            or command.dataset
            or command.analysis
        )
        if intent.status == ClaimStatus.SUPPORTED and not has_support:
            raise ValidationError("Supported claims require supporting datasets or analyses.")

    def _validate_visualization(self, command: RecordEvidenceBundleCommand) -> None:
        intent = command.visualization
        if intent is None:
            return
        if isinstance(intent, ExistingVisualizationIntent):
            self._visualization_in_project(
                intent.visualization_id,
                command.project_id,
                label="Visualization",
            )
            return
        analysis_id = intent.analysis_id
        if analysis_id is not None:
            self._project_component(
                entity_id=analysis_id,
                project_id=command.project_id,
                label="Visualization analysis",
                loader=self.analyses.get_analysis,
            )
        elif command.analysis is None:
            raise ValidationError("Visualization create requires an analysis reference.")
        for claim_id in intent.related_claim_ids:
            self._project_component(
                entity_id=claim_id,
                project_id=command.project_id,
                label="Related claim",
                loader=self.claims.get_claim,
            )

    def _validate_source_note(self, command: RecordEvidenceBundleCommand) -> None:
        intent = command.source_note
        if intent is None:
            return
        if isinstance(intent, ExistingSourceNoteIntent):
            self._project_component(
                entity_id=intent.note_id,
                project_id=command.project_id,
                label="Source note",
                loader=self.notes.get_note,
            )
            return
        if intent.targets is not None:
            for target in intent.targets:
                try:
                    self.notes.validate_target(target, command.project_id)
                except NotFoundError:
                    raise _component_not_found("Source note target") from None
                except ValidationError as exc:
                    if str(exc) == "Target must belong to the same project.":
                        raise _component_not_found("Source note target") from None
                    raise

    def _project_component(
        self,
        *,
        entity_id: UUID,
        project_id: UUID,
        label: str,
        loader: Any,
    ) -> Any:
        """Load a project-owned component without exposing cross-project IDs."""

        try:
            entity = loader(entity_id)
        except NotFoundError:
            raise _component_not_found(label) from None
        if getattr(entity, "project_id", None) != project_id:
            raise _component_not_found(label)
        return entity

    def _visualization_in_project(
        self,
        visualization_id: UUID,
        project_id: UUID,
        *,
        label: str,
    ) -> Any:
        """Load a visualization through its owning analysis without ID leakage."""

        try:
            visualization = self.visualizations.get_visualization(visualization_id)
            analysis = self.analyses.get_analysis(visualization.analysis_id)
        except NotFoundError:
            raise _component_not_found(label) from None
        if analysis.project_id != project_id:
            raise _component_not_found(label)
        return visualization

    def _preview(self, prepared: _PreparedEvidenceBundle) -> EvidenceBundleResult:
        command = prepared.command
        component_ids = self._existing_component_ids(command)
        return EvidenceBundleResult(
            outcome="preview",
            project_id=command.project_id,
            idempotency_key=_clean_optional(command.idempotency_key),
            component_ids=component_ids,
            plan=self._plan(command, component_ids, preview=True),
            warnings=self._warnings(command),
        )

    def _execute(
        self,
        prepared: _PreparedEvidenceBundle,
        *,
        actor: AuthContext,
    ) -> EvidenceBundleResult:
        command = prepared.command
        ids = self._existing_component_ids(command)

        if isinstance(command.dataset, CreateDatasetIntent):
            dataset = self.datasets.create_dataset(
                command.project_id,
                command.dataset.primary_question_id or command.primary_question_id,
                secondary_question_ids=command.dataset.secondary_question_ids,
                status=command.dataset.status,
                terminal_reason=command.dataset.terminal_reason,
                commit_manifest=command.dataset.commit_manifest,
                commit_hash=_clean_optional(command.dataset.commit_hash),
                actor=actor,
            )
            ids = replace(ids, dataset_id=dataset.dataset_id)

        if isinstance(command.analysis, CreateAnalysisIntent):
            analysis = self.analyses.create_analysis(
                command.project_id,
                _append_id(command.analysis.dataset_ids, ids.dataset_id),
                _clean_required(command.analysis.method_hash, "method_hash"),
                _clean_required(command.analysis.code_version, "code_version"),
                environment_hash=_clean_optional(command.analysis.environment_hash),
                external_artifacts=command.analysis.external_artifacts,
                status=command.analysis.status,
                terminal_reason=command.analysis.terminal_reason,
                actor=actor,
            )
            ids = replace(ids, analysis_id=analysis.analysis_id)

        if isinstance(command.claim, CreateClaimIntent):
            claim = self.claims.create_claim(
                command.project_id,
                _clean_required(command.claim.statement, "statement"),
                command.claim.confidence,
                status=command.claim.status,
                terminal_reason=command.claim.terminal_reason,
                falsification_criteria=command.claim.falsification_criteria,
                verification_plan=command.claim.verification_plan,
                refuting_outcome=command.claim.refuting_outcome,
                supported_by_dataset_ids=_append_id(
                    command.claim.supported_by_dataset_ids,
                    ids.dataset_id,
                ),
                supported_by_analysis_ids=_append_id(
                    command.claim.supported_by_analysis_ids,
                    ids.analysis_id,
                ),
                answers_question_ids=_append_id(
                    command.claim.answers_question_ids,
                    command.primary_question_id,
                ),
                external_citations=command.claim.external_citations,
                actor=actor,
            )
            ids = replace(ids, claim_id=claim.claim_id)

        if isinstance(command.visualization, CreateVisualizationIntent):
            visualization = self.visualizations.create_visualization(
                command.visualization.analysis_id or ids.analysis_id,
                _clean_required(command.visualization.viz_type, "viz_type"),
                _clean_required(command.visualization.file_path, "file_path"),
                caption=_clean_optional(command.visualization.caption),
                related_claim_ids=_append_id(
                    command.visualization.related_claim_ids,
                    ids.claim_id,
                ),
                actor=actor,
            )
            ids = replace(ids, visualization_id=visualization.viz_id)

        if isinstance(command.source_note, CreateSourceNoteIntent):
            note = self.notes.create_note(
                command.project_id,
                _clean_required(command.source_note.raw_content, "raw_content"),
                transcribed_text=_clean_optional(command.source_note.transcribed_text),
                targets=(
                    command.source_note.targets
                    if command.source_note.targets is not None
                    else self._default_note_targets(command, ids)
                ),
                metadata=dict(command.source_note.metadata),
                status=command.source_note.status,
                actor=actor,
            )
            ids = replace(ids, source_note_id=note.note_id)

        return EvidenceBundleResult(
            outcome="created",
            project_id=command.project_id,
            idempotency_key=_required_key(command.idempotency_key),
            component_ids=ids,
            plan=self._plan(command, ids, preview=False),
            warnings=self._warnings(command),
        )

    def _existing_component_ids(
        self,
        command: RecordEvidenceBundleCommand,
    ) -> EvidenceBundleComponentIds:
        return EvidenceBundleComponentIds(
            dataset_id=(
                command.dataset.dataset_id
                if isinstance(command.dataset, ExistingDatasetIntent)
                else None
            ),
            analysis_id=(
                command.analysis.analysis_id
                if isinstance(command.analysis, ExistingAnalysisIntent)
                else None
            ),
            claim_id=(
                command.claim.claim_id if isinstance(command.claim, ExistingClaimIntent) else None
            ),
            visualization_id=(
                command.visualization.visualization_id
                if isinstance(command.visualization, ExistingVisualizationIntent)
                else None
            ),
            source_note_id=(
                command.source_note.note_id
                if isinstance(command.source_note, ExistingSourceNoteIntent)
                else None
            ),
        )

    def _plan(
        self,
        command: RecordEvidenceBundleCommand,
        ids: EvidenceBundleComponentIds,
        *,
        preview: bool,
    ) -> tuple[EvidenceBundlePlanStep, ...]:
        steps: list[EvidenceBundlePlanStep] = []
        values = (
            (
                "dataset",
                command.dataset,
                ids.dataset_id,
                _canonical_dataset(command.dataset, command.primary_question_id),
            ),
            (
                "analysis",
                command.analysis,
                ids.analysis_id,
                _canonical_analysis(
                    command.analysis,
                    _bundle_dataset_ref(command.dataset),
                ),
            ),
            (
                "claim",
                command.claim,
                ids.claim_id,
                _canonical_claim(
                    command.claim,
                    dataset_ref=_bundle_dataset_ref(command.dataset),
                    analysis_ref=_bundle_analysis_ref(command.analysis),
                    primary_question_id=command.primary_question_id,
                ),
            ),
            (
                "visualization",
                command.visualization,
                ids.visualization_id,
                _canonical_visualization(
                    command.visualization,
                    analysis_ref=_bundle_analysis_ref(command.analysis),
                    claim_ref=_bundle_claim_ref(command.claim),
                ),
            ),
            (
                "source_note",
                command.source_note,
                ids.source_note_id,
                _canonical_source_note(
                    command.source_note,
                    default_target=_default_canonical_note_target(
                        command,
                        dataset_ref=_bundle_dataset_ref(command.dataset),
                        analysis_ref=_bundle_analysis_ref(command.analysis),
                        claim_ref=_bundle_claim_ref(command.claim),
                    ),
                ),
            ),
        )
        existing_types = (
            ExistingDatasetIntent,
            ExistingAnalysisIntent,
            ExistingClaimIntent,
            ExistingVisualizationIntent,
            ExistingSourceNoteIntent,
        )
        for entity_type, intent, entity_id, details in values:
            if intent is None:
                continue
            reused = isinstance(intent, existing_types)
            steps.append(
                EvidenceBundlePlanStep(
                    action="reuse" if reused else "create",
                    entity_type=entity_type,
                    entity_id=entity_id if reused or not preview else None,
                    reason="provided_existing_id" if reused else None,
                    details=details,
                )
            )
        return tuple(steps)

    def _warnings(self, command: RecordEvidenceBundleCommand) -> tuple[str, ...]:
        warnings: list[str] = []
        if (
            isinstance(command.analysis, CreateAnalysisIntent)
            and command.analysis.derive_code_provenance
        ):
            warnings.append(
                "Code provenance is not derived server-side; supplied provenance fields were used."
            )
        upload_intent = getattr(command.visualization, "upload_intent", None)
        if upload_intent is not None:
            warnings.append(
                "Visualization attachment upload is performed by the client after bundle commit."
            )
        return tuple(warnings)

    def _default_note_targets(
        self,
        command: RecordEvidenceBundleCommand,
        ids: EvidenceBundleComponentIds,
    ) -> tuple[EntityRef, ...]:
        choices = (
            (EntityType.VISUALIZATION, ids.visualization_id),
            (EntityType.CLAIM, ids.claim_id),
            (EntityType.ANALYSIS, ids.analysis_id),
            (EntityType.DATASET, ids.dataset_id),
            (EntityType.QUESTION, command.primary_question_id),
        )
        for entity_type, entity_id in choices:
            if entity_id is not None:
                return (EntityRef(entity_type=entity_type, entity_id=entity_id),)
        return ()

    def _compare_replay(
        self,
        record: EvidenceBundleRecord,
        request_fingerprint: str,
    ) -> EvidenceBundleResult:
        if record.request_fingerprint != request_fingerprint:
            raise ConflictError(
                "Evidence bundle idempotency key was already used with conflicting fields."
            )
        return EvidenceBundleResult.from_record(record)

    def _fingerprint(self, command: RecordEvidenceBundleCommand) -> str:
        canonical = _canonical_command(command)
        encoded = json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _normalize_command(
    command: RecordEvidenceBundleCommand,
) -> RecordEvidenceBundleCommand:
    return replace(
        command,
        dataset=_normalize_dataset_intent(
            command.dataset,
            fallback_question_id=command.primary_question_id,
        ),
        analysis=_normalize_analysis_intent(command.analysis),
        claim=_normalize_claim_intent(command.claim),
        visualization=_normalize_visualization_intent(command.visualization),
        source_note=_normalize_source_note_intent(command.source_note),
        idempotency_key=_normalize_idempotency_key(
            command.idempotency_key,
            required=not command.dry_run,
        ),
    )


def _normalize_dataset_intent(
    intent: DatasetIntent | None,
    *,
    fallback_question_id: UUID | None,
) -> DatasetIntent | None:
    if intent is None or isinstance(intent, ExistingDatasetIntent):
        return intent
    primary_question_id = intent.primary_question_id or fallback_question_id
    if primary_question_id is None:
        raise ValidationError("Dataset create requires primary_question_id.")
    secondary_question_ids = tuple(unique_ids(intent.secondary_question_ids))
    if primary_question_id in secondary_question_ids:
        raise ValidationError("Primary question cannot be secondary.")
    _reject_reserved_metadata_key(intent.commit_manifest)
    manifest_file_sizes = _normalized_manifest_file_sizes(intent)
    question_links = [
        QuestionLink(
            question_id=primary_question_id,
            role=QuestionLinkRole.PRIMARY,
        ),
        *[
            QuestionLink(question_id=question_id, role=QuestionLinkRole.SECONDARY)
            for question_id in secondary_question_ids
        ],
    ]
    manifest = build_commit_manifest(intent.commit_manifest, question_links)
    commit_hash = compute_commit_hash(manifest)
    validate_commit_hash(intent.commit_hash, commit_hash)
    terminal_reason = terminal_reason_for_status(
        None,
        intent.status,
        DatasetStatus.ARCHIVED,
        intent.terminal_reason,
        entity_name="Dataset",
    )
    return replace(
        intent,
        primary_question_id=primary_question_id,
        secondary_question_ids=secondary_question_ids,
        commit_manifest=manifest,
        commit_hash=commit_hash,
        terminal_reason=terminal_reason,
        manifest_file_sizes=manifest_file_sizes,
    )


def _normalized_manifest_file_sizes(
    intent: CreateDatasetIntent,
) -> tuple[tuple[str, int | None], ...]:
    if intent.manifest_file_sizes:
        return tuple(sorted(intent.manifest_file_sizes))
    manifest = intent.commit_manifest
    files = manifest.files if manifest is not None else []
    resolved: list[tuple[str, int | None]] = []
    for file in files:
        if file.size_bytes is not None and file.size_bytes < 0:
            raise ValidationError("file.size_bytes must not be negative.")
        resolved.append((file.path.strip(), file.size_bytes))
    return tuple(sorted(resolved))


def _normalize_analysis_intent(
    intent: AnalysisIntent | None,
) -> AnalysisIntent | None:
    if intent is None or isinstance(intent, ExistingAnalysisIntent):
        return intent
    return replace(
        intent,
        dataset_ids=tuple(unique_ids(intent.dataset_ids)),
        method_hash=_clean_required(intent.method_hash, "method_hash"),
        code_version=_clean_required(intent.code_version, "code_version"),
        environment_hash=_clean_optional(intent.environment_hash),
        external_artifacts=_normalize_external_references(
            intent.external_artifacts,
            duplicate_message="Duplicate external artifact reference on analysis.",
        ),
        terminal_reason=terminal_reason_for_status(
            None,
            intent.status,
            AnalysisStatus.ARCHIVED,
            intent.terminal_reason,
            entity_name="Analysis",
        ),
    )


def _normalize_claim_intent(intent: ClaimIntent | None) -> ClaimIntent | None:
    if intent is None or isinstance(intent, ExistingClaimIntent):
        return intent
    if not math.isfinite(intent.confidence) or not 0 <= intent.confidence <= 100:
        raise ValidationError("confidence must be a finite value between 0 and 100.")
    return replace(
        intent,
        statement=_clean_required(intent.statement, "statement"),
        terminal_reason=terminal_reason_for_status(
            None,
            intent.status,
            ClaimStatus.REJECTED,
            intent.terminal_reason,
            entity_name="Claim",
        ),
        falsification_criteria=_clean_optional(intent.falsification_criteria),
        verification_plan=_clean_optional(intent.verification_plan),
        refuting_outcome=_clean_optional(intent.refuting_outcome),
        supported_by_dataset_ids=tuple(unique_ids(intent.supported_by_dataset_ids)),
        supported_by_analysis_ids=tuple(unique_ids(intent.supported_by_analysis_ids)),
        answers_question_ids=tuple(unique_ids(intent.answers_question_ids)),
        external_citations=_normalize_external_references(
            intent.external_citations,
            duplicate_message="Duplicate external citation reference on claim.",
        ),
    )


def _normalize_visualization_intent(
    intent: VisualizationIntent | None,
) -> VisualizationIntent | None:
    if intent is None:
        return None
    upload_intent = _normalize_upload_intent(intent.upload_intent)
    if isinstance(intent, ExistingVisualizationIntent):
        return replace(intent, upload_intent=upload_intent)
    return replace(
        intent,
        viz_type=_clean_required(intent.viz_type, "viz_type"),
        file_path=_clean_required(intent.file_path, "file_path"),
        caption=_clean_optional(intent.caption),
        related_claim_ids=tuple(unique_ids(intent.related_claim_ids)),
        upload_intent=upload_intent,
    )


def _normalize_source_note_intent(
    intent: SourceNoteIntent | None,
) -> SourceNoteIntent | None:
    if intent is None or isinstance(intent, ExistingSourceNoteIntent):
        return intent
    raw_metadata = dict(intent.metadata)
    _reject_reserved_metadata_key(raw_metadata)
    targets = intent.targets
    if targets is not None:
        target_keys = [(target.entity_type, target.entity_id) for target in targets]
        if len(set(target_keys)) != len(target_keys):
            raise ValidationError("Duplicate source-note target in evidence bundle.")
    return replace(
        intent,
        raw_content=_clean_required(intent.raw_content, "raw_content"),
        transcribed_text=_clean_optional(intent.transcribed_text),
        targets=tuple(targets) if targets is not None else None,
        metadata=tuple(sorted(normalize_note_metadata(raw_metadata).items())),
    )


def _normalize_upload_intent(
    intent: EvidenceBundleUploadIntent | None,
) -> EvidenceBundleUploadIntent | None:
    if intent is None:
        return None
    checksum = intent.checksum_sha256.strip().lower()
    if _SHA256_RE.fullmatch(checksum) is None:
        raise ValidationError("upload_intent checksum_sha256 must be a SHA-256 hex digest.")
    if intent.size_bytes <= 0:
        raise ValidationError("upload_intent size_bytes must be greater than zero.")
    return replace(
        intent,
        checksum_sha256=checksum,
        filename=_clean_required(intent.filename, "upload_intent filename"),
        content_type=validate_upload_content_type(intent.content_type),
    )


def _normalize_external_references(
    values: tuple[ExternalArtifactReference, ...],
    *,
    duplicate_message: str,
) -> tuple[ExternalArtifactReference, ...]:
    normalized: list[ExternalArtifactReference] = []
    seen: set[tuple[str, str, str]] = set()
    for value in values:
        item = ExternalArtifactReference.model_validate(value)
        reason = external_artifact_uri_validation_error(item.uri)
        if reason is not None:
            raise ValidationError(reason)
        key = (item.kind.value, item.source_system, item.uri)
        if key in seen:
            raise ValidationError(duplicate_message)
        seen.add(key)
        normalized.append(item)
    return tuple(normalized)


def _normalize_idempotency_key(value: str | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ValidationError("idempotency_key must not be empty.")
        return None
    cleaned = value.strip()
    if not cleaned:
        raise ValidationError("idempotency_key must not be empty.")
    if len(cleaned) > 200:
        raise ValidationError("idempotency_key must be 200 characters or fewer.")
    return cleaned


def _reject_reserved_metadata_key(value: object) -> None:
    if isinstance(value, BaseModel):
        _reject_reserved_metadata_key(value.model_dump(mode="python"))
        return
    if isinstance(value, dict):
        if EVIDENCE_BUNDLE_IDEMPOTENCY_METADATA_KEY in {str(key) for key in value}:
            raise ValidationError(
                f"{EVIDENCE_BUNDLE_IDEMPOTENCY_METADATA_KEY!r} is reserved and "
                "must not be persisted in evidence metadata."
            )
        for item in value.values():
            _reject_reserved_metadata_key(item)
        return
    if isinstance(value, tuple | list):
        for item in value:
            _reject_reserved_metadata_key(item)


def _bundle_dataset_ref(intent: DatasetIntent | None) -> str | None:
    if isinstance(intent, ExistingDatasetIntent):
        return str(intent.dataset_id)
    return "$bundle.dataset" if isinstance(intent, CreateDatasetIntent) else None


def _bundle_analysis_ref(intent: AnalysisIntent | None) -> str | None:
    if isinstance(intent, ExistingAnalysisIntent):
        return str(intent.analysis_id)
    return "$bundle.analysis" if isinstance(intent, CreateAnalysisIntent) else None


def _bundle_claim_ref(intent: ClaimIntent | None) -> str | None:
    if isinstance(intent, ExistingClaimIntent):
        return str(intent.claim_id)
    return "$bundle.claim" if isinstance(intent, CreateClaimIntent) else None


def _canonical_external_references(
    values: tuple[ExternalArtifactReference, ...],
) -> list[dict[str, Any]]:
    payloads = [item.model_dump(mode="json", exclude_none=True) for item in values]
    return sorted(
        payloads,
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )


def _canonical_command(command: RecordEvidenceBundleCommand) -> dict[str, Any]:
    dataset_ref = _bundle_dataset_ref(command.dataset)
    analysis_ref = _bundle_analysis_ref(command.analysis)
    claim_ref = _bundle_claim_ref(command.claim)

    payload: dict[str, Any] = {
        "project_id": str(command.project_id),
        "primary_question_id": _uuid_text(command.primary_question_id),
        "dataset": _canonical_dataset(command.dataset, command.primary_question_id),
        "analysis": _canonical_analysis(command.analysis, dataset_ref),
        "claim": _canonical_claim(
            command.claim,
            dataset_ref=dataset_ref,
            analysis_ref=analysis_ref,
            primary_question_id=command.primary_question_id,
        ),
        "visualization": _canonical_visualization(
            command.visualization,
            analysis_ref=analysis_ref,
            claim_ref=claim_ref,
        ),
        "source_note": _canonical_source_note(
            command.source_note,
            default_target=_default_canonical_note_target(
                command,
                dataset_ref=dataset_ref,
                analysis_ref=analysis_ref,
                claim_ref=claim_ref,
            ),
        ),
    }
    return _jsonable(payload)


def _canonical_dataset(
    intent: DatasetIntent | None,
    fallback_question_id: UUID | None,
) -> dict[str, Any] | None:
    if intent is None:
        return None
    if isinstance(intent, ExistingDatasetIntent):
        return {"kind": "existing", "dataset_id": str(intent.dataset_id)}
    if not isinstance(intent.commit_manifest, DatasetCommitManifest):
        raise RuntimeError("Evidence-bundle dataset intent was not normalized.")
    return {
        "kind": "create",
        "primary_question_id": _uuid_text(intent.primary_question_id or fallback_question_id),
        "secondary_question_ids": _sorted_uuid_text(intent.secondary_question_ids),
        "commit_manifest": dataset_manifest_payload(intent.commit_manifest),
        "manifest_file_sizes": [
            {"path": path, "size_bytes": size_bytes}
            for path, size_bytes in intent.manifest_file_sizes
        ],
        "commit_hash": intent.commit_hash,
        "status": intent.status,
        "terminal_reason": _clean_optional(intent.terminal_reason),
    }


def _canonical_analysis(
    intent: AnalysisIntent | None,
    dataset_ref: str | None,
) -> dict[str, Any] | None:
    if intent is None:
        return None
    if isinstance(intent, ExistingAnalysisIntent):
        return {"kind": "existing", "analysis_id": str(intent.analysis_id)}
    dataset_ids = {str(item) for item in intent.dataset_ids}
    if dataset_ref is not None:
        dataset_ids.add(dataset_ref)
    return {
        "kind": "create",
        "dataset_ids": sorted(dataset_ids),
        "method_hash": _clean_required(intent.method_hash, "method_hash"),
        "code_version": _clean_required(intent.code_version, "code_version"),
        "environment_hash": _clean_optional(intent.environment_hash),
        "external_artifacts": _canonical_external_references(intent.external_artifacts),
        "status": intent.status,
        "terminal_reason": _clean_optional(intent.terminal_reason),
        "derive_code_provenance": intent.derive_code_provenance,
    }


def _canonical_claim(
    intent: ClaimIntent | None,
    *,
    dataset_ref: str | None,
    analysis_ref: str | None,
    primary_question_id: UUID | None,
) -> dict[str, Any] | None:
    if intent is None:
        return None
    if isinstance(intent, ExistingClaimIntent):
        return {"kind": "existing", "claim_id": str(intent.claim_id)}
    dataset_ids = {str(item) for item in intent.supported_by_dataset_ids}
    analysis_ids = {str(item) for item in intent.supported_by_analysis_ids}
    question_ids = {str(item) for item in intent.answers_question_ids}
    if dataset_ref is not None:
        dataset_ids.add(dataset_ref)
    if analysis_ref is not None:
        analysis_ids.add(analysis_ref)
    if primary_question_id is not None:
        question_ids.add(str(primary_question_id))
    return {
        "kind": "create",
        "statement": _clean_required(intent.statement, "statement"),
        "confidence": intent.confidence,
        "status": intent.status,
        "terminal_reason": _clean_optional(intent.terminal_reason),
        "falsification_criteria": _clean_optional(intent.falsification_criteria),
        "verification_plan": _clean_optional(intent.verification_plan),
        "refuting_outcome": _clean_optional(intent.refuting_outcome),
        "supported_by_dataset_ids": sorted(dataset_ids),
        "supported_by_analysis_ids": sorted(analysis_ids),
        "answers_question_ids": sorted(question_ids),
        "external_citations": _canonical_external_references(intent.external_citations),
    }


def _canonical_visualization(
    intent: VisualizationIntent | None,
    *,
    analysis_ref: str | None,
    claim_ref: str | None,
) -> dict[str, Any] | None:
    if intent is None:
        return None
    if isinstance(intent, ExistingVisualizationIntent):
        return {
            "kind": "existing",
            "viz_id": str(intent.visualization_id),
            "upload_intent": intent.upload_intent,
        }
    claim_ids = {str(item) for item in intent.related_claim_ids}
    if claim_ref is not None:
        claim_ids.add(claim_ref)
    return {
        "kind": "create",
        "analysis_id": _uuid_text(intent.analysis_id) or analysis_ref,
        "viz_type": _clean_required(intent.viz_type, "viz_type"),
        "file_path": _clean_required(intent.file_path, "file_path"),
        "caption": _clean_optional(intent.caption),
        "related_claim_ids": sorted(claim_ids),
        "upload_intent": intent.upload_intent,
    }


def _canonical_source_note(
    intent: SourceNoteIntent | None,
    *,
    default_target: dict[str, str] | None,
) -> dict[str, Any] | None:
    if intent is None:
        return None
    if isinstance(intent, ExistingSourceNoteIntent):
        return {"kind": "existing", "note_id": str(intent.note_id)}
    targets = (
        sorted(
            (
                {
                    "entity_type": target.entity_type.value,
                    "entity_id": str(target.entity_id),
                }
                for target in intent.targets
            ),
            key=lambda item: (item["entity_type"], item["entity_id"]),
        )
        if intent.targets is not None
        else ([default_target] if default_target is not None else [])
    )
    return {
        "kind": "create",
        "raw_content": _clean_required(intent.raw_content, "raw_content"),
        "transcribed_text": _clean_optional(intent.transcribed_text),
        "targets": targets,
        "metadata": dict(sorted(intent.metadata)),
        "status": intent.status,
    }


def _default_canonical_note_target(
    command: RecordEvidenceBundleCommand,
    *,
    dataset_ref: str | None,
    analysis_ref: str | None,
    claim_ref: str | None,
) -> dict[str, str] | None:
    visualization_ref: str | None = None
    if isinstance(command.visualization, ExistingVisualizationIntent):
        visualization_ref = str(command.visualization.visualization_id)
    elif isinstance(command.visualization, CreateVisualizationIntent):
        visualization_ref = "$bundle.visualization"
    choices = (
        ("visualization", visualization_ref),
        ("claim", claim_ref),
        ("analysis", analysis_ref),
        ("dataset", dataset_ref),
        ("question", _uuid_text(command.primary_question_id)),
    )
    for entity_type, entity_id in choices:
        if entity_id is not None:
            return {"entity_type": entity_type, "entity_id": entity_id}
    return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json", exclude_none=False))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, EvidenceBundleUploadIntent):
        return {
            "checksum_sha256": value.checksum_sha256.lower(),
            "size_bytes": value.size_bytes,
            "filename": value.filename.strip(),
            "content_type": value.content_type.strip().lower(),
        }
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    return value


def _append_id(values: tuple[UUID, ...], candidate: UUID | None) -> list[UUID]:
    resolved = list(dict.fromkeys(values))
    if candidate is not None and candidate not in resolved:
        resolved.append(candidate)
    return resolved


def _sorted_uuid_text(values: tuple[UUID, ...]) -> list[str]:
    return sorted(str(value) for value in set(values))


def _component_not_found(label: str) -> NotFoundError:
    return NotFoundError(f"{label} does not exist in the bundle project.")


def _required_actor_id(actor: AuthContext | None) -> str:
    resolved = actor_user_id(actor)
    if resolved is None:
        raise ValidationError("Evidence bundles require an authenticated principal.")
    if len(resolved) > 255:
        raise ValidationError("Authenticated principal identifier is too long.")
    return resolved


def _required_key(value: str | None) -> str:
    resolved = _normalize_idempotency_key(value, required=True)
    if resolved is None:  # pragma: no cover - guarded by ``required=True``
        raise ValidationError("idempotency_key must not be empty.")
    return resolved


def _clean_required(value: str | None, field_name: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValidationError(f"{field_name} must not be empty.")
    return cleaned


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _uuid_text(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _optional_uuid(value: Any) -> UUID | None:
    if value in {None, ""}:
        return None
    return UUID(str(value))
