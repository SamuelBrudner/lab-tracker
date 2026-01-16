"""Error types for lab tracker."""


class LabTrackerError(Exception):
    """Base exception for lab tracker."""


class ValidationError(LabTrackerError):
    """Invalid input or state."""


class NotFoundError(LabTrackerError):
    """Requested entity does not exist."""


class AuthError(LabTrackerError):
    """Authentication or authorization failure."""


class ConflictError(LabTrackerError):
    """Conflicting state or duplicate entity."""
