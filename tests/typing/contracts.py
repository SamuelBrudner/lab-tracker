"""Compile-only structural contracts for the incremental architecture boundary.

This module is a mypy target, not a pytest module.  Passing concrete adapters
and services to these typed sinks proves conformance without adding nominal
inheritance or runtime checks.
"""

from lab_tracker.api import LabTrackerAPI
from lab_tracker.app_parts.runtime import AppRuntime
from lab_tracker.application.catalog_queries import CatalogAccess, CatalogRepository
from lab_tracker.application.context_queries import ContextAccess, ContextRepository
from lab_tracker.application.file_commands import (
    DatasetFileLocking,
    DatasetFileRepository,
    FileCommandAccess,
    FileStorage,
)
from lab_tracker.application.handlers import RequestHandlerApi, RequestHandlerRepository
from lab_tracker.application.managed_deletions import (
    DeleteStorage,
    ManagedDeletionAccess,
)
from lab_tracker.decision_context_query import DecisionContextRepository
from lab_tracker.file_storage import FileStorageBackend, LocalFileStorageBackend
from lab_tracker.graph_drafting import (
    AgenticGraphDraftClient,
    AnthropicGraphDraftClient,
    GoogleGraphDraftClient,
    GraphDraftClient,
    GraphDraftClientFactory,
    OpenAIGraphDraftClient,
    make_graph_draft_client,
)
from lab_tracker.local_filesystem_operations import (
    BoundedLocalFilesystemOperations,
)
from lab_tracker.local_filesystem_ports import LocalDirectoryInspector
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.project_graph import ProjectGraphRepository
from lab_tracker.repository import LabTrackerRepository
from lab_tracker.services import (
    GraphContextBuilder,
    GraphDraftGenerationCoordinator,
    GraphDraftRecords,
    GraphPatchValidator,
    ProjectAuthorizationPolicy,
)
from lab_tracker.services.graph_draft_commit import (
    CommitAuthorization,
    CommitDatasets,
    CommitPatchApplier,
    CommitQuestions,
    CommitRecords,
    CommitRepository,
    CommitVersions,
)
from lab_tracker.services.graph_draft_generation import (
    GenerationAuthorization,
    GenerationContextBuilder,
    GenerationNotes,
    GenerationPatchValidator,
    GenerationRecords,
)
from lab_tracker.services.graph_draft_review import (
    ReviewAuthorization,
    ReviewPatchValidator,
    ReviewRecords,
    RevisionGenerator,
)
from lab_tracker.services.graph_draft_scheduling_ports import (
    BatchDraftGenerator,
    BatchRunStore,
    BatchSettingsStore,
    SchedulingAuthorization,
    SchedulingNotes,
    SchedulingProjects,
    SchedulingProvenanceLinks,
    SchedulingRecords,
    SchedulingRepository,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


def _requires_repository(value: LabTrackerRepository) -> None:
    pass


def _requires_catalog_repository(value: CatalogRepository) -> None:
    pass


def _requires_context_repository(value: ContextRepository) -> None:
    pass


def _requires_dataset_file_repository(value: DatasetFileRepository) -> None:
    pass


def _requires_dataset_file_locking(value: DatasetFileLocking) -> None:
    pass


def _requires_decision_context_repository(value: DecisionContextRepository) -> None:
    pass


def _requires_project_graph_repository(value: ProjectGraphRepository) -> None:
    pass


def _requires_handler_repository(value: RequestHandlerRepository) -> None:
    pass


def repository_contracts(repository: SQLAlchemyLabTrackerRepository) -> None:
    """The SQLAlchemy adapter conforms structurally to broad and local ports."""

    _requires_repository(repository)
    _requires_catalog_repository(repository)
    _requires_context_repository(repository)
    _requires_dataset_file_repository(repository)
    _requires_dataset_file_locking(repository)
    _requires_decision_context_repository(repository)
    _requires_project_graph_repository(repository)
    _requires_handler_repository(repository)


def _requires_catalog_access(value: CatalogAccess) -> None:
    pass


def _requires_context_access(value: ContextAccess) -> None:
    pass


def _requires_file_access(value: FileCommandAccess) -> None:
    pass


def _requires_deletion_access(value: ManagedDeletionAccess) -> None:
    pass


def _requires_handler_api(value: RequestHandlerApi) -> None:
    pass


def application_contracts(api: LabTrackerAPI) -> None:
    _requires_catalog_access(api)
    _requires_context_access(api)
    _requires_file_access(api)
    _requires_deletion_access(api)
    _requires_handler_api(api)


def _requires_graph_client(value: GraphDraftClient) -> None:
    pass


def _requires_graph_client_factory(value: GraphDraftClientFactory) -> None:
    pass


def provider_contracts(
    openai_client: OpenAIGraphDraftClient,
    anthropic_client: AnthropicGraphDraftClient,
    google_client: GoogleGraphDraftClient,
    agentic_client: AgenticGraphDraftClient,
) -> None:
    _requires_graph_client(openai_client)
    _requires_graph_client(anthropic_client)
    _requires_graph_client(google_client)
    _requires_graph_client(agentic_client)
    _requires_graph_client_factory(make_graph_draft_client)


def runtime_factory_contract(runtime: AppRuntime) -> None:
    _requires_graph_client_factory(runtime.graph_draft_client_factory)


def _requires_local_directory_inspector(value: LocalDirectoryInspector) -> None:
    pass


def local_filesystem_contracts(
    operations: BoundedLocalFilesystemOperations,
    runtime: AppRuntime,
) -> None:
    _requires_local_directory_inspector(operations)
    _requires_local_directory_inspector(runtime.local_filesystem_operations)


def _requires_file_storage(value: FileStorage) -> None:
    pass


def _requires_delete_storage(value: DeleteStorage) -> None:
    pass


def storage_contracts(
    file_storage: FileStorageBackend,
    local_file_storage: LocalFileStorageBackend,
    raw_note_storage: LocalNoteStorage,
) -> None:
    _requires_file_storage(file_storage)
    _requires_file_storage(local_file_storage)
    _requires_delete_storage(raw_note_storage)


def runtime_storage_contracts(runtime: AppRuntime) -> None:
    _requires_file_storage(runtime.file_storage_backend)
    _requires_delete_storage(runtime.raw_note_storage)


def _requires_generation_records(value: GenerationRecords) -> None:
    pass


def _requires_review_records(value: ReviewRecords) -> None:
    pass


def _requires_commit_records(value: CommitRecords) -> None:
    pass


def _requires_scheduling_records(value: SchedulingRecords) -> None:
    pass


def records_contracts(records: GraphDraftRecords) -> None:
    _requires_generation_records(records)
    _requires_review_records(records)
    _requires_commit_records(records)
    _requires_scheduling_records(records)


def _requires_generation_notes(value: GenerationNotes) -> None:
    pass


def _requires_scheduling_notes(value: SchedulingNotes) -> None:
    pass


def _requires_generation_authorization(value: GenerationAuthorization) -> None:
    pass


def _requires_review_authorization(value: ReviewAuthorization) -> None:
    pass


def _requires_commit_authorization(value: CommitAuthorization) -> None:
    pass


def _requires_scheduling_authorization(value: SchedulingAuthorization) -> None:
    pass


def _requires_generation_context(value: GenerationContextBuilder) -> None:
    pass


def _requires_generation_validator(value: GenerationPatchValidator) -> None:
    pass


def _requires_review_validator(value: ReviewPatchValidator) -> None:
    pass


def collaborator_contracts(
    api: LabTrackerAPI,
    authorization: ProjectAuthorizationPolicy,
    context_builder: GraphContextBuilder,
    validator: GraphPatchValidator,
) -> None:
    _requires_generation_notes(api.notes)
    _requires_scheduling_notes(api.notes)
    _requires_generation_authorization(authorization)
    _requires_review_authorization(authorization)
    _requires_commit_authorization(authorization)
    _requires_scheduling_authorization(authorization)
    _requires_generation_context(context_builder)
    _requires_generation_validator(validator)
    _requires_review_validator(validator)


def _requires_revision_generator(value: RevisionGenerator) -> None:
    pass


def _requires_batch_generator(value: BatchDraftGenerator) -> None:
    pass


def generation_contracts(generation: GraphDraftGenerationCoordinator) -> None:
    _requires_revision_generator(generation)
    _requires_batch_generator(generation)


def _requires_commit_patch_applier(value: CommitPatchApplier) -> None:
    pass


def _requires_commit_versions(value: CommitVersions) -> None:
    pass


def _requires_commit_questions(value: CommitQuestions) -> None:
    pass


def _requires_commit_datasets(value: CommitDatasets) -> None:
    pass


def _requires_scheduling_projects(value: SchedulingProjects) -> None:
    pass


def _requires_scheduling_provenance(value: SchedulingProvenanceLinks) -> None:
    pass


def _requires_batch_settings_store(value: BatchSettingsStore) -> None:
    pass


def _requires_batch_run_store(value: BatchRunStore) -> None:
    pass


def remaining_collaborator_contracts(api: LabTrackerAPI) -> None:
    _requires_commit_patch_applier(api.graph_drafts.commit.patch_applier)
    _requires_commit_versions(api.entity_versions)
    _requires_commit_questions(api.questions)
    _requires_commit_datasets(api.datasets)
    _requires_scheduling_projects(api.projects)
    _requires_scheduling_provenance(api.provenance_links)


def _requires_commit_repository(value: CommitRepository) -> None:
    pass


def _requires_scheduling_repository(value: SchedulingRepository) -> None:
    pass


def graph_repository_contracts(repository: SQLAlchemyLabTrackerRepository) -> None:
    _requires_commit_repository(repository)
    _requires_scheduling_repository(repository)
    _requires_batch_settings_store(repository.graph_draft_batch_settings)
    _requires_batch_run_store(repository.graph_draft_batch_runs)
