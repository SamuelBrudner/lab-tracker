"""Import-stable compatibility barrel for SQLAlchemy repository classes.

The implementation lives under :mod:`lab_tracker.sqlalchemy_repository_parts`.
Keep this module available for retained-v1 callers and tests; new internal code may
import focused repository modules directly when that makes ownership clearer.
"""

from lab_tracker.sqlalchemy_repository_parts import (
    SQLAlchemyAcquisitionCollectionRepository,
    SQLAlchemyAcquisitionOutputRepository,
    SQLAlchemyAnalysisRepository,
    SQLAlchemyClaimRepository,
    SQLAlchemyDatasetRepository,
    SQLAlchemyEntityVersionRepository,
    SQLAlchemyEvidenceBundleRepository,
    SQLAlchemyExperimentRepository,
    SQLAlchemyExplorationNodeRepository,
    SQLAlchemyGoalRepository,
    SQLAlchemyGroupMembershipRepository,
    SQLAlchemyLabTrackerRepository,
    SQLAlchemyNoteRepository,
    SQLAlchemyOwnershipReassignmentRepository,
    SQLAlchemyProjectGroupRepository,
    SQLAlchemyProjectMembershipRepository,
    SQLAlchemyProjectRepository,
    SQLAlchemyQuestionRepository,
    SQLAlchemyRecordExportEventRepository,
    SQLAlchemySessionRepository,
    SQLAlchemySupervisionEdgeRepository,
    SQLAlchemyVisualizationRepository,
)

__all__ = [
    "SQLAlchemyAcquisitionCollectionRepository",
    "SQLAlchemyAcquisitionOutputRepository",
    "SQLAlchemyAnalysisRepository",
    "SQLAlchemyClaimRepository",
    "SQLAlchemyDatasetRepository",
    "SQLAlchemyEntityVersionRepository",
    "SQLAlchemyEvidenceBundleRepository",
    "SQLAlchemyExperimentRepository",
    "SQLAlchemyExplorationNodeRepository",
    "SQLAlchemyGoalRepository",
    "SQLAlchemyGroupMembershipRepository",
    "SQLAlchemyLabTrackerRepository",
    "SQLAlchemyNoteRepository",
    "SQLAlchemyOwnershipReassignmentRepository",
    "SQLAlchemyProjectGroupRepository",
    "SQLAlchemyProjectMembershipRepository",
    "SQLAlchemyProjectRepository",
    "SQLAlchemyQuestionRepository",
    "SQLAlchemyRecordExportEventRepository",
    "SQLAlchemySessionRepository",
    "SQLAlchemySupervisionEdgeRepository",
    "SQLAlchemyVisualizationRepository",
]
