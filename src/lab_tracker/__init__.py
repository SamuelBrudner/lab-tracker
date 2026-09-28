"""Lab tracker package.

The public names re-exported here resolve lazily, on first attribute access.
Every ``lab_tracker.<submodule>`` import runs this file first, and
``lab_tracker_client`` imports light server modules (``lab_tracker.models``,
``lab_tracker._version``) inside users' processes: IPython startup, the scripts
``.pth`` hook, the Jupyter save hook. Eagerly importing ``LabTrackerAPI`` here
would drag the API, auth, SQLAlchemy, and FastAPI into each of them.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from lab_tracker._version import __version__

if TYPE_CHECKING:
    from lab_tracker.acquisition_watcher import AcquisitionOutputWatcher
    from lab_tracker.api import LabTrackerAPI
    from lab_tracker.auth import AuthContext, AuthService, Role, require_role
    from lab_tracker.errors import (
        AuthError,
        ConflictError,
        LabTrackerError,
        NotFoundError,
        PermissionDeniedError,
        StoreAuthorityDeniedError,
        ValidationError,
    )
    from lab_tracker.models import (
        AcquisitionOutput,
        Analysis,
        AnalysisStatus,
        Claim,
        ClaimStatus,
        Dataset,
        DatasetStatus,
        EntityRef,
        EntityType,
        GroupMembership,
        Note,
        NoteRawAsset,
        NoteStatus,
        OutcomeStatus,
        Project,
        ProjectGroup,
        ProjectGroupKind,
        ProjectMembership,
        ProjectMembershipRole,
        ProjectStatus,
        Question,
        QuestionLink,
        QuestionLinkRole,
        QuestionRefactor,
        QuestionStatus,
        QuestionType,
        Session,
        SessionStatus,
        SessionType,
        SupervisionEdge,
        Visualization,
    )

# Defining module -> the public names re-exported from it.
_LAZY_EXPORTS_BY_MODULE: dict[str, tuple[str, ...]] = {
    "lab_tracker.acquisition_watcher": ("AcquisitionOutputWatcher",),
    "lab_tracker.api": ("LabTrackerAPI",),
    "lab_tracker.auth": ("AuthContext", "AuthService", "Role", "require_role"),
    "lab_tracker.errors": (
        "AuthError",
        "ConflictError",
        "LabTrackerError",
        "NotFoundError",
        "PermissionDeniedError",
        "StoreAuthorityDeniedError",
        "ValidationError",
    ),
    "lab_tracker.models": (
        "AcquisitionOutput",
        "Analysis",
        "AnalysisStatus",
        "Claim",
        "ClaimStatus",
        "Dataset",
        "DatasetStatus",
        "EntityRef",
        "EntityType",
        "GroupMembership",
        "Note",
        "NoteRawAsset",
        "NoteStatus",
        "OutcomeStatus",
        "Project",
        "ProjectGroup",
        "ProjectGroupKind",
        "ProjectMembership",
        "ProjectMembershipRole",
        "ProjectStatus",
        "Question",
        "QuestionLink",
        "QuestionLinkRole",
        "QuestionRefactor",
        "QuestionStatus",
        "QuestionType",
        "Session",
        "SessionStatus",
        "SessionType",
        "SupervisionEdge",
        "Visualization",
    ),
}
_LAZY_EXPORTS: dict[str, str] = {
    name: module for module, names in _LAZY_EXPORTS_BY_MODULE.items() for name in names
}

# Hidden from type checkers, which read the TYPE_CHECKING imports above: a
# visible module ``__getattr__`` would make every unknown ``lab_tracker.X``
# type as ``Any`` instead of an error.
if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Import a re-exported name from its defining module on first access."""

        module_name = _LAZY_EXPORTS.get(name)
        if module_name is None:
            # AttributeError also lets ``from lab_tracker import <submodule>``
            # fall through to importing the submodule.
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(importlib.import_module(module_name), name)
        globals()[name] = value
        return value


def __dir__() -> list[str]:
    """List the lazily re-exported names alongside the module's own globals."""

    return sorted({*globals(), *__all__})


__all__ = [
    "__version__",
    "Analysis",
    "AnalysisStatus",
    "AcquisitionOutput",
    "AcquisitionOutputWatcher",
    "AuthContext",
    "AuthError",
    "AuthService",
    "Claim",
    "ClaimStatus",
    "ConflictError",
    "Dataset",
    "DatasetStatus",
    "EntityRef",
    "EntityType",
    "GroupMembership",
    "LabTrackerAPI",
    "LabTrackerError",
    "Note",
    "NoteRawAsset",
    "NoteStatus",
    "NotFoundError",
    "OutcomeStatus",
    "PermissionDeniedError",
    "Project",
    "ProjectGroup",
    "ProjectGroupKind",
    "ProjectMembership",
    "ProjectMembershipRole",
    "ProjectStatus",
    "Question",
    "QuestionRefactor",
    "QuestionLink",
    "QuestionLinkRole",
    "QuestionStatus",
    "QuestionType",
    "Role",
    "Session",
    "SessionStatus",
    "SessionType",
    "StoreAuthorityDeniedError",
    "SupervisionEdge",
    "ValidationError",
    "Visualization",
    "require_role",
]
