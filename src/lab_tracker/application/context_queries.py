"""Typed application queries that assemble cross-aggregate read models."""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy.orm import Session as OrmSession

from lab_tracker.artifact_resolution import (
    DEFAULT_MAX_BYTES,
    PreparedArtifactResolutionTarget,
    ResolvedArtifact,
    ResolverRegistry,
    git_store_resolution_target,
    http_store_resolution_target,
    is_rclone_store_kind,
    local_store_resolution_target,
    rclone_store_resolution_target,
    resolver_registry_for_prepared_target,
    unresolved,
)
from lab_tracker.auth import AuthContext
from lab_tracker.decision_context import JsonObject, build_decision_context
from lab_tracker.decision_context_query import (
    DecisionContextRepository,
    RepositoryDecisionContextReader,
)
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.git_store_locator import (
    PinnedGitPath,
    canonical_git_store_uri,
)
from lab_tracker.local_store_locator import (
    LocalStoreLocator,
    PortableStorePath,
    canonical_local_store_uri,
    canonical_store_uri,
    is_valid_store_name,
    parse_canonical_store_authority,
)
from lab_tracker.models import (
    Analysis,
    Claim,
    ClaimEdge,
    Dataset,
    DataStore,
    EntityType,
    ExplorationNode,
    ExternalArtifactReference,
    Goal,
    Project,
    ProjectStatus,
    Question,
    StoreKind,
    SupervisionEdge,
    UsageEventResourceType,
    UsageEventVerb,
)
from lab_tracker.portfolio_query import portfolio_summary_groups
from lab_tracker.project_graph import build_project_graph
from lab_tracker.provenance import (
    build_analysis_provenance_document,
    build_claim_provenance_document,
    build_dataset_provenance_document,
)
from lab_tracker.schemas import (
    AssistantDecisionContextRequest,
    PortfolioProjectGroupSummary,
    ProjectGraphRead,
    ProjectGraphView,
    SearchResults,
)

from .types import Page

ExternalArtifactEntityType = Literal["analysis", "claim", "dataset"]


@dataclass(frozen=True)
class PreparedExternalArtifactResolution:
    """A database-free external-artifact resolution plan.

    Preparation authorizes and materializes the target while the request scope is
    open. Resolution then releases that scope before invoking an external resolver.
    """

    entity_type: ExternalArtifactEntityType
    entity_id: UUID
    artifact_index: int
    target: PreparedArtifactResolutionTarget
    max_bytes: int
    byte_range: tuple[int, int] | None


class ContextDataStoreLookup(Protocol):
    def get_by_name(self, project_id: UUID, name: str) -> DataStore | None: ...


class ContextAccess(Protocol):
    """Domain reads and authorization needed by cross-aggregate queries."""

    def accessible_project_ids(self, actor: AuthContext | None) -> set[UUID] | None: ...

    def require_project_read(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> None: ...

    def get_project(self, project_id: UUID) -> Project: ...

    def get_project_for_read(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Project: ...

    def get_dataset(self, dataset_id: UUID) -> Dataset: ...

    def get_dataset_for_read(
        self,
        dataset_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Dataset: ...

    def get_analysis(self, analysis_id: UUID) -> Analysis: ...

    def get_analysis_for_read(
        self,
        analysis_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Analysis: ...

    def get_claim(self, claim_id: UUID) -> Claim: ...

    def get_claim_for_read(
        self,
        claim_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Claim: ...

    def get_question(self, question_id: UUID) -> Question: ...

    def get_goal(self, goal_id: UUID) -> Goal: ...

    def require_goal_read(
        self,
        goal: Goal,
        *,
        actor: AuthContext | None,
    ) -> set[UUID]: ...

    def record_usage_event(
        self,
        *,
        verb: UsageEventVerb | str,
        resource_type: UsageEventResourceType | str,
        project_id: UUID | None,
        actor: AuthContext | None,
        result_count: int | None,
    ) -> None: ...


class ContextRepository(DecisionContextRepository, Protocol):
    """Repository roles used directly or by delegated context projections."""

    @property
    def data_stores(self) -> ContextDataStoreLookup: ...

    def query_supervision_edges(
        self,
        *,
        limit: int | None,
        offset: int,
    ) -> tuple[list[SupervisionEdge], int]: ...

    def query_claim_edges(
        self,
        *,
        project_id: UUID,
        limit: int | None,
        offset: int,
    ) -> tuple[list[ClaimEdge], int]: ...

    def query_goals(
        self,
        *,
        project_id: UUID,
        limit: int | None,
        offset: int,
    ) -> tuple[list[Goal], int]: ...

    def query_exploration_nodes(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        node_type: str | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        created_by: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[ExplorationNode], int]: ...


@dataclass(frozen=True)
class ContextQueries:
    """Cross-aggregate reads hidden behind a typed application boundary."""

    api: ContextAccess
    repository: ContextRepository
    session: OrmSession
    release_read_scope: Callable[[], None]
    resolver_registry: ResolverRegistry | None = None

    def decision_context(
        self,
        payload: AssistantDecisionContextRequest,
        *,
        actor: AuthContext,
    ) -> JsonObject:
        accessible_project_ids = self.api.accessible_project_ids(actor)
        resolved_project_id = payload.project_id
        if resolved_project_id is not None:
            self.api.require_project_read(resolved_project_id, actor=actor)
        elif accessible_project_ids is not None and len(accessible_project_ids) == 1:
            resolved_project_id = next(iter(accessible_project_ids))
        reader = RepositoryDecisionContextReader(
            self.repository,
            accessible_project_ids=accessible_project_ids,
        )
        return build_decision_context(
            reader,
            task_kind=payload.task_kind,
            query=payload.query,
            project_id=str(resolved_project_id) if resolved_project_id else None,
            question_id=str(payload.question_id) if payload.question_id else None,
            dataset_id=str(payload.dataset_id) if payload.dataset_id else None,
            analysis_id=str(payload.analysis_id) if payload.analysis_id else None,
            claim_id=str(payload.claim_id) if payload.claim_id else None,
            visualization_id=(
                str(payload.visualization_id) if payload.visualization_id else None
            ),
            created_by=str(payload.created_by) if payload.created_by else None,
            since=payload.since,
            until=payload.until,
            limit=payload.limit,
        )

    def portfolio_summary(
        self,
        *,
        actor: AuthContext,
        status: ProjectStatus | None,
        limit: int,
        offset: int,
    ) -> Page[PortfolioProjectGroupSummary]:
        groups, total = portfolio_summary_groups(
            self.session,
            accessible_project_ids=self.api.accessible_project_ids(actor),
            status=status,
            limit=limit,
            offset=offset,
        )
        return Page(items=groups, total=total)

    def project_graph(
        self,
        project_id: UUID,
        *,
        actor: AuthContext,
        view: ProjectGraphView,
    ) -> ProjectGraphRead:
        project = self.api.get_project_for_read(project_id, actor=actor)
        return build_project_graph(self.repository, project.project_id, view=view)

    def dataset_provenance(
        self,
        dataset_id: UUID,
        *,
        actor: AuthContext,
        base_url: str,
    ) -> dict[str, object]:
        dataset = self.api.get_dataset_for_read(dataset_id, actor=actor)
        supervision_edges, _ = self.repository.query_supervision_edges(
            limit=None,
            offset=0,
        )
        return build_dataset_provenance_document(
            base_url,
            dataset,
            supervision_edges=supervision_edges,
        )

    def analysis_provenance(
        self,
        analysis_id: UUID,
        *,
        actor: AuthContext,
        base_url: str,
    ) -> dict[str, object]:
        analysis = self.api.get_analysis_for_read(analysis_id, actor=actor)
        datasets = [
            self.api.get_dataset(dataset_id) for dataset_id in analysis.dataset_ids
        ]
        claims, _ = self.repository.query_claims(
            analysis_id=analysis_id,
            limit=None,
            offset=0,
        )
        claim_ids = {claim.claim_id for claim in claims}
        claim_edges, _ = self.repository.query_claim_edges(
            project_id=analysis.project_id,
            limit=None,
            offset=0,
        )
        claim_edges = [edge for edge in claim_edges if edge.claim_id in claim_ids]
        visualizations, _ = self.repository.query_visualizations(
            analysis_id=analysis_id,
            limit=None,
            offset=0,
        )
        supervision_edges, _ = self.repository.query_supervision_edges(
            limit=None,
            offset=0,
        )
        return build_analysis_provenance_document(
            base_url,
            analysis,
            datasets=datasets,
            claims=claims,
            visualizations=visualizations,
            claim_edges=claim_edges,
            supervision_edges=supervision_edges,
        )

    def claim_provenance(
        self,
        claim_id: UUID,
        *,
        actor: AuthContext,
        base_url: str,
    ) -> dict[str, object]:
        claim = self.api.get_claim_for_read(claim_id, actor=actor)
        analyses = [
            self.api.get_analysis(analysis_id)
            for analysis_id in claim.supported_by_analysis_ids
        ]
        dataset_ids = set(claim.supported_by_dataset_ids)
        for analysis in analyses:
            dataset_ids.update(analysis.dataset_ids)
        datasets = [
            self.api.get_dataset(dataset_id) for dataset_id in sorted(dataset_ids)
        ]
        question_ids = set(claim.answers_question_ids)
        for dataset in datasets:
            question_ids.update(link.question_id for link in dataset.question_links)
        questions = [
            self.api.get_question(question_id) for question_id in sorted(question_ids)
        ]
        visualizations, _ = self.repository.query_visualizations(
            claim_id=claim_id,
            limit=None,
            offset=0,
        )
        claim_edges, _ = self.repository.query_claim_edges(
            project_id=claim.project_id,
            limit=None,
            offset=0,
        )
        claim_edges = [
            edge
            for edge in claim_edges
            if edge.claim_id == claim_id or edge.target_claim_id == claim_id
        ]
        supervision_edges, _ = self.repository.query_supervision_edges(
            limit=None,
            offset=0,
        )
        return build_claim_provenance_document(
            base_url,
            claim,
            analyses=analyses,
            datasets=datasets,
            questions=questions,
            visualizations=visualizations,
            claim_edges=claim_edges,
            supervision_edges=supervision_edges,
        )

    def search(
        self,
        *,
        actor: AuthContext,
        query: str,
        project_id: UUID | None,
        goal_id: UUID | None,
        include: str | None,
        limit: int,
        offset: int,
    ) -> SearchResults:
        include_set = {
            item.strip().casefold()
            for item in (include.split(",") if include else ["questions", "notes"])
            if item.strip()
        }
        project_ids: set[UUID] | None
        linked_question_ids: set[UUID] | None = None
        linked_note_ids: set[UUID] | None = None
        if goal_id is not None:
            goal = self.api.get_goal(goal_id)
            goal_project_ids = self.api.require_goal_read(goal, actor=actor)
            if project_id is not None and not _goal_matches_project(
                goal,
                project_id=project_id,
                scope_project_ids=goal_project_ids,
            ):
                # Preserve the existing project mismatch behavior without
                # revealing it to callers who cannot read the full goal.
                self.api.require_project_read(project_id, actor=actor)
                raise ValidationError("goal_id must belong to project_id.")
            project_ids = {project_id} if project_id is not None else goal_project_ids
            linked_question_ids = {
                link.target.entity_id
                for link in goal.links
                if link.target.entity_type == EntityType.QUESTION
            }
            linked_note_ids = {
                link.target.entity_id
                for link in goal.links
                if link.target.entity_type == EntityType.NOTE
            }
        elif project_id is not None:
            self.api.require_project_read(project_id, actor=actor)
            project_ids = {project_id}
        else:
            project_ids = self.api.accessible_project_ids(actor)
        questions = (
            self.repository.query_questions(
                project_id=None,
                project_ids=project_ids,
                question_ids=linked_question_ids,
                search=query,
                limit=limit,
                offset=offset,
            )[0]
            if not include_set or "questions" in include_set
            else []
        )
        notes = (
            self.repository.query_notes(
                project_id=None,
                project_ids=project_ids,
                note_ids=linked_note_ids,
                search=query,
                limit=limit,
                offset=offset,
            )[0]
            if not include_set or "notes" in include_set
            else []
        )
        self.api.record_usage_event(
            verb=UsageEventVerb.SEARCH,
            resource_type=UsageEventResourceType.SEARCH,
            project_id=_single_project_id(project_ids),
            actor=actor,
            result_count=len(questions) + len(notes),
        )
        return SearchResults(questions=questions, notes=notes)

    def prepare_external_artifact_resolution(
        self,
        *,
        actor: AuthContext,
        entity_type: ExternalArtifactEntityType,
        entity_id: UUID,
        artifact_index: int,
        content_hash: str | None,
        max_bytes: int | None,
        byte_start: int | None,
        byte_end: int | None,
    ) -> PreparedExternalArtifactResolution:
        byte_range: tuple[int, int] | None = None
        if byte_start is not None or byte_end is not None:
            if byte_start is None or byte_end is None:
                raise ValidationError(
                    "byte_start and byte_end must be provided together."
                )
            if byte_end < byte_start:
                raise ValidationError("byte_end must be greater than or equal to byte_start.")
            byte_range = (byte_start, byte_end)

        reference, project_id = self._locate_external_reference(
            actor=actor,
            entity_type=entity_type,
            entity_id=entity_id,
            artifact_index=artifact_index,
            content_hash=content_hash,
        )

        detached_reference = reference.model_copy(deep=True)
        materialized = self._materialize_reference(detached_reference, project_id)
        target = (
            materialized.model_copy(deep=True)
            if isinstance(materialized, ExternalArtifactReference)
            else materialized
        )
        return PreparedExternalArtifactResolution(
            entity_type=entity_type,
            entity_id=entity_id,
            artifact_index=artifact_index,
            target=target,
            max_bytes=max_bytes or DEFAULT_MAX_BYTES,
            byte_range=byte_range,
        )

    def resolve_prepared_external_artifact(
        self,
        prepared: PreparedExternalArtifactResolution,
    ) -> dict[str, Any]:
        """Release SQLAlchemy resources, then resolve a detached target only."""

        self.release_read_scope()
        registry = resolver_registry_for_prepared_target(
            prepared.target,
            configured=self.resolver_registry,
        )
        result = registry.resolve_prepared(
            prepared.target,
            max_bytes=prepared.max_bytes,
            byte_range=prepared.byte_range,
        )
        body = result.to_json_dict()
        body["entity_type"] = prepared.entity_type
        body["entity_id"] = str(prepared.entity_id)
        body["artifact_index"] = prepared.artifact_index
        body["content_base64"] = (
            base64.b64encode(result.content).decode("ascii")
            if result.is_verified and result.content is not None
            else None
        )
        return body

    def _locate_external_reference(
        self,
        *,
        actor: AuthContext,
        entity_type: ExternalArtifactEntityType,
        entity_id: UUID,
        artifact_index: int,
        content_hash: str | None,
    ) -> tuple[ExternalArtifactReference, UUID]:
        artifacts, project_id = self._entity_artifacts(
            actor=actor,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        if artifact_index >= len(artifacts):
            raise NotFoundError(
                f"No external artifact at index {artifact_index} on "
                f"{entity_type} {entity_id}."
            )
        reference = artifacts[artifact_index]
        if content_hash is not None and content_hash != reference.content_hash:
            raise ValidationError(
                "content_hash does not match the artifact at the given index."
            )
        return reference, project_id

    def _entity_artifacts(
        self,
        *,
        actor: AuthContext,
        entity_type: ExternalArtifactEntityType,
        entity_id: UUID,
    ) -> tuple[list[ExternalArtifactReference], UUID]:
        if entity_type == "analysis":
            analysis = self.api.get_analysis_for_read(entity_id, actor=actor)
            return list(analysis.external_artifacts), analysis.project_id
        if entity_type == "claim":
            claim = self.api.get_claim_for_read(entity_id, actor=actor)
            return list(claim.external_citations), claim.project_id
        dataset = self.api.get_dataset_for_read(entity_id, actor=actor)
        manifest = dataset.commit_manifest
        artifacts = list(manifest.external_artifacts) if manifest is not None else []
        return artifacts, dataset.project_id

    def _materialize_reference(
        self,
        reference: ExternalArtifactReference,
        project_id: UUID,
    ) -> PreparedArtifactResolutionTarget:
        if reference.store_name is not None and reference.locator is not None:
            return self._resolve_store(
                reference,
                project_id,
                (reference.store_name,),
                reference.locator,
                locator_is_uri_path=False,
            )
        parsed_store = _parse_store_reference_uri(reference.uri)
        if parsed_store is None:
            if (
                reference.source_system.lower() == "store"
                or _looks_like_store_uri(reference.uri)
            ):
                return _invalid_store_reference(reference)
            return reference
        return self._resolve_store(
            reference,
            project_id,
            parsed_store.name_candidates,
            parsed_store.locator,
            locator_is_uri_path=True,
        )

    def _resolve_store(
        self,
        reference: ExternalArtifactReference,
        project_id: UUID,
        name_candidates: tuple[str, ...],
        locator: str,
        *,
        locator_is_uri_path: bool,
    ) -> PreparedArtifactResolutionTarget:
        matches: dict[UUID, tuple[str, DataStore]] = {}
        for candidate in dict.fromkeys(name_candidates):
            candidate_store = self.repository.data_stores.get_by_name(
                project_id, candidate
            )
            if candidate_store is None or candidate_store.name != candidate:
                continue
            matches[candidate_store.store_id] = (candidate, candidate_store)
        if not matches:
            return _unavailable_store_reference(reference)
        if len(matches) != 1:
            return _invalid_store_reference(reference)
        name, store = next(iter(matches.values()))

        concrete: PreparedArtifactResolutionTarget | None
        if store.kind is StoreKind.LOCAL_FS:
            parsed_locator = (
                LocalStoreLocator.parse_uri_path(locator)
                if locator_is_uri_path
                else LocalStoreLocator.parse_decoded(locator)
            )
            canonical_uri = (
                canonical_local_store_uri(name, parsed_locator)
                if parsed_locator is not None
                else None
            )
            if (
                parsed_locator is None
                or canonical_uri != reference.uri
            ):
                return _invalid_store_reference(reference)
            logical_reference = reference.model_copy(
                update={
                    "source_system": "store",
                    "store_name": name,
                    "locator": parsed_locator.path,
                },
                deep=True,
            )
            concrete = local_store_resolution_target(
                store,
                locator=parsed_locator,
                logical_reference=logical_reference,
            )
        elif store.kind is StoreKind.HTTP:
            parsed_locator = (
                PortableStorePath.parse_uri_path(locator)
                if locator_is_uri_path
                else PortableStorePath.parse_decoded(locator)
            )
            canonical_reference = _nonlocal_store_logical_reference(
                reference,
                store_name=name,
                locator=parsed_locator,
                locator_is_uri_path=locator_is_uri_path,
            )
            if parsed_locator is None or canonical_reference is None:
                return _invalid_store_reference(reference)
            concrete = http_store_resolution_target(
                store,
                locator=parsed_locator,
                logical_reference=canonical_reference,
            )
        elif is_rclone_store_kind(store.kind):
            parsed_locator = (
                PortableStorePath.parse_uri_path(locator)
                if locator_is_uri_path
                else PortableStorePath.parse_decoded(locator)
            )
            canonical_reference = _nonlocal_store_logical_reference(
                reference,
                store_name=name,
                locator=parsed_locator,
                locator_is_uri_path=locator_is_uri_path,
            )
            if parsed_locator is None or canonical_reference is None:
                return _invalid_store_reference(reference)
            concrete = rclone_store_resolution_target(
                store,
                locator=parsed_locator,
                logical_reference=canonical_reference,
            )
        elif store.kind is StoreKind.GIT:
            pin = (
                PinnedGitPath.parse_uri_path(locator)
                if locator_is_uri_path
                else PinnedGitPath.parse_decoded(locator)
            )
            canonical_reference = _git_store_logical_reference(
                reference,
                store_name=name,
                pin=pin,
                locator_is_uri_path=locator_is_uri_path,
            )
            if pin is None or canonical_reference is None:
                return _invalid_store_reference(reference)
            concrete = git_store_resolution_target(
                store,
                pin=pin,
                logical_reference=canonical_reference,
            )
        else:
            concrete = None
        if concrete is None:
            return _unavailable_store_reference(reference)
        return concrete


def _goal_matches_project(
    goal: Goal,
    *,
    project_id: UUID,
    scope_project_ids: set[UUID],
) -> bool:
    if goal.project_id is not None:
        return goal.project_id == project_id
    return project_id in scope_project_ids


def _single_project_id(project_ids: set[UUID] | None) -> UUID | None:
    if project_ids is None or len(project_ids) != 1:
        return None
    return next(iter(project_ids))


@dataclass(frozen=True, slots=True)
class _ParsedStoreReferenceUri:
    name_candidates: tuple[str, ...]
    locator: str


def _nonlocal_store_logical_reference(
    reference: ExternalArtifactReference,
    *,
    store_name: str,
    locator: PortableStorePath | None,
    locator_is_uri_path: bool,
) -> ExternalArtifactReference | None:
    """Canonicalize one validated nonlocal ``store://`` identity.

    Structured legacy references may retain a decoded display URI, while URI-only
    references may retain a literal legacy authority. Both forms resolve to the
    same canonical detached identity after an exact match.
    """

    canonical_uri = (
        canonical_store_uri(store_name, locator) if locator is not None else None
    )
    if canonical_uri is None or locator is None:
        return None
    accepted_uris = {
        canonical_uri,
        f"store://{store_name}/{locator.uri_path}",
    }
    if not locator_is_uri_path:
        accepted_uris.add(f"store://{store_name}/{locator.path}")
    if reference.uri not in accepted_uris:
        return None
    return reference.model_copy(
        update={
            "source_system": "store",
            "uri": canonical_uri,
            "store_name": store_name,
            "locator": locator.path,
        },
        deep=True,
    )


def _git_store_logical_reference(
    reference: ExternalArtifactReference,
    *,
    store_name: str,
    pin: PinnedGitPath | None,
    locator_is_uri_path: bool,
) -> ExternalArtifactReference | None:
    """Canonicalize one validated immutable registered-Git identity."""

    canonical_uri = (
        canonical_git_store_uri(store_name, pin) if pin is not None else None
    )
    if canonical_uri is None or pin is None:
        return None
    accepted_uris = {
        canonical_uri,
        f"store://{store_name}/{pin.uri_path}",
    }
    if not locator_is_uri_path:
        accepted_uris.add(f"store://{store_name}/{pin.locator}")
    if reference.uri not in accepted_uris:
        return None
    return reference.model_copy(
        update={
            "source_system": "store",
            "uri": canonical_uri,
            "store_name": store_name,
            "locator": pin.locator,
        },
        deep=True,
    )


def _parse_store_reference_uri(uri: str) -> _ParsedStoreReferenceUri | None:
    """Retain legacy non-local URI parsing until kind-specific hardening lands."""

    try:
        parsed = urlsplit(uri)
    except ValueError:
        return None
    if parsed.scheme.lower() != "store" or not parsed.netloc:
        return None
    canonical_name = parse_canonical_store_authority(parsed.netloc)
    name_candidates = tuple(
        dict.fromkeys(
            candidate
            for candidate in (
                canonical_name,
                parsed.netloc if is_valid_store_name(parsed.netloc) else None,
            )
            if candidate is not None
        )
    )
    if not name_candidates:
        return None
    return _ParsedStoreReferenceUri(
        name_candidates=name_candidates,
        locator=parsed.path.lstrip("/"),
    )


def _looks_like_store_uri(uri: str) -> bool:
    """Classify malformed store-shaped input without reflecting its contents."""

    return isinstance(uri, str) and uri.lstrip().lower().startswith("store:")


def _safe_store_reference(reference: ExternalArtifactReference) -> ExternalArtifactReference:
    """Keep store failures static without reflecting a locator or registered root."""

    return ExternalArtifactReference(
        kind=reference.kind,
        source_system="store",
        uri="store://[redacted]",
        content_hash=reference.content_hash,
    )


def _invalid_store_reference(reference: ExternalArtifactReference) -> ResolvedArtifact:
    return unresolved(
        _safe_store_reference(reference),
        detail="Store artifact reference is invalid.",
    )


def _unavailable_store_reference(reference: ExternalArtifactReference) -> ResolvedArtifact:
    return unresolved(
        _safe_store_reference(reference),
        detail="Store artifact could not be resolved.",
    )
