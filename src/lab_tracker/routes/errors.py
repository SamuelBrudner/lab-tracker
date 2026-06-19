"""Exception handler registration for HTTP routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette import status as http_status
from starlette.requests import Request
from starlette.responses import JSONResponse

from lab_tracker.errors import (
    AuthError,
    ConflictError,
    LabTrackerError,
    NotFoundError,
    PayloadTooLargeError,
    RateLimitError,
    ValidationError,
)
from lab_tracker.schemas import ErrorEnvelope, ErrorInfo, ErrorIssue

_logger = logging.getLogger(__name__)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ValidationError)
    def _handle_validation_error(request: Request, exc: ValidationError):
        _log_handled_error(
            request,
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="validation_error",
            exc=exc,
        )
        return error_response(
            http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_error",
            str(exc),
        )

    @app.exception_handler(PayloadTooLargeError)
    def _handle_payload_too_large_error(request: Request, exc: PayloadTooLargeError):
        _log_handled_error(
            request,
            status_code=http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            code="payload_too_large",
            exc=exc,
        )
        return error_response(
            http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "payload_too_large",
            str(exc),
        )

    @app.exception_handler(NotFoundError)
    def _handle_not_found_error(request: Request, exc: NotFoundError):
        _log_handled_error(
            request,
            status_code=http_status.HTTP_404_NOT_FOUND,
            code="not_found",
            exc=exc,
        )
        return error_response(http_status.HTTP_404_NOT_FOUND, "not_found", str(exc))

    @app.exception_handler(AuthError)
    def _handle_auth_error(request: Request, exc: AuthError):
        _log_handled_error(
            request,
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            code="auth_error",
            exc=exc,
        )
        return error_response(http_status.HTTP_401_UNAUTHORIZED, "auth_error", str(exc))

    @app.exception_handler(RateLimitError)
    def _handle_rate_limit_error(request: Request, exc: RateLimitError):
        _log_handled_error(
            request,
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            code="rate_limited",
            exc=exc,
        )
        return error_response(
            http_status.HTTP_429_TOO_MANY_REQUESTS,
            "rate_limited",
            str(exc),
        )

    @app.exception_handler(ConflictError)
    def _handle_conflict_error(request: Request, exc: ConflictError):
        _log_handled_error(
            request,
            status_code=http_status.HTTP_409_CONFLICT,
            code="conflict",
            exc=exc,
        )
        return error_response(http_status.HTTP_409_CONFLICT, "conflict", str(exc))

    @app.exception_handler(LabTrackerError)
    def _handle_lab_tracker_error(request: Request, exc: LabTrackerError):
        _log_handled_error(
            request,
            status_code=http_status.HTTP_400_BAD_REQUEST,
            code="lab_tracker_error",
            exc=exc,
        )
        return error_response(http_status.HTTP_400_BAD_REQUEST, "lab_tracker_error", str(exc))

    @app.exception_handler(RequestValidationError)
    def _handle_request_validation_error(request: Request, exc: RequestValidationError):
        _log_handled_error(
            request,
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="request_validation_error",
            exc=exc,
        )
        return error_response(
            http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            "request_validation_error",
            "Request validation failed.",
            issues=issues_from_validation_errors(exc.errors()),
        )

    @app.exception_handler(Exception)
    def _handle_unhandled_error(request: Request, exc: Exception):
        _logger.error(
            "Unhandled HTTP exception: method=%s path=%s status_code=%s",
            request.method,
            request.url.path,
            http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return error_response(
            http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_server_error",
            "Internal server error.",
        )


def _log_handled_error(
    request: Request,
    *,
    status_code: int,
    code: str,
    exc: Exception,
) -> None:
    _logger.warning(
        "Handled HTTP error: method=%s path=%s status_code=%s code=%s detail=%s",
        request.method,
        request.url.path,
        status_code,
        code,
        exc,
    )


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    issues: list[ErrorIssue] | None = None,
) -> JSONResponse:
    payload = ErrorEnvelope(error=ErrorInfo(code=code, message=message, issues=issues))
    return JSONResponse(status_code=status_code, content=payload.model_dump())


def issues_from_validation_errors(errors: list[dict[str, Any]]) -> list[ErrorIssue]:
    issues: list[ErrorIssue] = []
    for error in errors:
        loc_parts = [str(part) for part in error.get("loc", []) if part != "body"]
        field = ".".join(loc_parts) if loc_parts else None
        issues.append(ErrorIssue(field=field, message=error.get("msg", "Invalid value")))
    return issues
