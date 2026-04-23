"""Exception handler registration for HTTP routes."""

from __future__ import annotations

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
    ValidationError,
)
from lab_tracker.schemas import ErrorEnvelope, ErrorInfo, ErrorIssue


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ValidationError)
    def _handle_validation_error(request: Request, exc: ValidationError):
        return error_response(
            http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_error",
            str(exc),
        )

    @app.exception_handler(NotFoundError)
    def _handle_not_found_error(request: Request, exc: NotFoundError):
        return error_response(http_status.HTTP_404_NOT_FOUND, "not_found", str(exc))

    @app.exception_handler(AuthError)
    def _handle_auth_error(request: Request, exc: AuthError):
        return error_response(http_status.HTTP_401_UNAUTHORIZED, "auth_error", str(exc))

    @app.exception_handler(ConflictError)
    def _handle_conflict_error(request: Request, exc: ConflictError):
        return error_response(http_status.HTTP_409_CONFLICT, "conflict", str(exc))

    @app.exception_handler(LabTrackerError)
    def _handle_lab_tracker_error(request: Request, exc: LabTrackerError):
        return error_response(http_status.HTTP_400_BAD_REQUEST, "lab_tracker_error", str(exc))

    @app.exception_handler(RequestValidationError)
    def _handle_request_validation_error(request: Request, exc: RequestValidationError):
        return error_response(
            http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            "request_validation_error",
            "Request validation failed.",
            issues=issues_from_validation_errors(exc.errors()),
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
