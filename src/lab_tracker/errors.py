"""Error types for lab tracker."""


class LabTrackerError(Exception):
    """Base exception for lab tracker."""


class ValidationError(LabTrackerError):
    """Invalid input or state."""


class NotFoundError(LabTrackerError):
    """Requested entity does not exist."""


class OpaqueTargetNotFoundError(NotFoundError):
    """A read target is deliberately indistinguishable from an inaccessible one."""


class AuthError(LabTrackerError):
    """Authentication or authorization failure."""


class StoreAuthorityDeniedError(LabTrackerError):
    """A data-store grant did not authorize the requested registration."""


class DataStorePersistenceError(LabTrackerError):
    """A data-store registration failed after admission."""

    def __init__(self) -> None:
        super().__init__("Data store registration could not be completed.")


class ConflictError(LabTrackerError):
    """Conflicting state or duplicate entity."""


class PayloadTooLargeError(ValidationError):
    """Uploaded payload exceeds the configured size limit."""


class RateLimitError(AuthError):
    """Too many authentication attempts in a short window."""
