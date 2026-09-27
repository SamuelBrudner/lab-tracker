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
    """Authentication failure: the credential is missing, invalid, or rejected.

    Maps to HTTP ``401 auth_error``; clients may refresh the credential or sign
    the user out. Authorization denials for an authenticated principal use the
    :class:`PermissionDeniedError` subtype instead.
    """


class PermissionDeniedError(AuthError):
    """An authenticated principal lacks permission for the requested action.

    Maps to HTTP ``403 forbidden``; the credential stays valid, so clients must
    not refresh it or sign the user out. It subclasses :class:`AuthError` so
    existing ``except AuthError`` sites (for example opaque-read conversion to
    ``404``) keep treating it as an access failure.
    """


class ServiceScopeDeniedError(PermissionDeniedError):
    """An lpat_ token's scope forbids this request body or parameter.

    The route path itself is allowed for the token (the middleware policy let
    the request through), but what it asks for — a committed note status, an
    evidence bundle with ``dry_run=false`` — exceeds the scope. Maps to HTTP
    ``403 service_forbidden`` so MCP clients steer to a capable credential
    (``use_capable_credential``) instead of requesting project access.
    """


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
