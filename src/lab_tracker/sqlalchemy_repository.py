"""Import-stable compatibility barrel for SQLAlchemy repository classes.

The implementation lives under :mod:`lab_tracker.sqlalchemy_repository_parts`.
Keep this module available for retained-v1 callers and tests; new internal code may
import focused repository modules directly when that makes ownership clearer.
"""

from lab_tracker.sqlalchemy_repository_parts import (
    SQLAlchemyAcquisitionOutputRepository,
    SQLAlchemyAnalysisRepository,
    SQLAlchemyClaimRepository,
    SQLAlchemyDatasetRepository,
    SQLAlchemyLabTrackerRepository,
    SQLAlchemyNoteRepository,
    SQLAlchemyProjectRepository,
    SQLAlchemyQuestionRepository,
    SQLAlchemySessionRepository,
    SQLAlchemyVisualizationRepository,
)

__all__ = [
    "SQLAlchemyAcquisitionOutputRepository",
    "SQLAlchemyAnalysisRepository",
    "SQLAlchemyClaimRepository",
    "SQLAlchemyDatasetRepository",
    "SQLAlchemyLabTrackerRepository",
    "SQLAlchemyNoteRepository",
    "SQLAlchemyProjectRepository",
    "SQLAlchemyQuestionRepository",
    "SQLAlchemySessionRepository",
    "SQLAlchemyVisualizationRepository",
]
