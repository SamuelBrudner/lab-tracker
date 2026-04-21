"""Compatibility wrapper for HTTP route registration."""

from __future__ import annotations

from fastapi import FastAPI

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthService, TokenService
from lab_tracker.routes import register_routes as register_feature_routes


def register_routes(
    app: FastAPI,
    api: LabTrackerAPI,
    *,
    auth_service: AuthService,
    token_service: TokenService,
    bootstrap_admin_token: str | None = None,
) -> None:
    register_feature_routes(
        app,
        api,
        auth_service=auth_service,
        token_service=token_service,
        bootstrap_admin_token=bootstrap_admin_token,
    )
