"""MCP resource functions and registration."""

from __future__ import annotations

from typing import Any

from lab_tracker.decision_context import AGENT_CONSULTATION_POLICY
from lab_tracker.decision_context_constants import code_facing_idioms


def lab_tracker_quickstart() -> str:
    return (
        "# Lab Tracker MCP Quickstart\n\n"
        "Use `lab_tracker_health` and `lab_tracker_readiness` first. "
        "Read and write tools call the running Lab Tracker API, so start the app "
        "and set `LAB_TRACKER_MCP_BASE_URL` in the MCP client environment. "
        "Use `http://127.0.0.1:8000` only when the MCP process runs on the same "
        "machine as the API; remote agents should use the workstation HTTPS "
        "endpoint, for example `https://<workstation>.ts.net`. "
        "`LAB_TRACKER_MCP_USERNAME` and `LAB_TRACKER_MCP_PASSWORD` are only "
        "required when API authentication is enabled. Notes use `staged`, "
        "`committed`, or `archived` status. Note metadata values may be strings, "
        "numbers, or booleans and are stored as strings. Evidence authoring "
        "uses first-class create tools for datasets, analyses, claims, and "
        "visualizations; create or reuse datasets before analyses, analyses "
        "before supported claims or visualizations, upload visualization files "
        "into managed storage when local figure assets exist, and verify the "
        "graph with list tools.\n"
    )


def lab_tracker_surface() -> str:
    return (
        "Lab Tracker v1 preserves projects, questions, notes, sessions, datasets, "
        "analyses, claims, and visualizations. The supported runtime surface is "
        "documented in `docs/retained-v1-surface.md`.\n"
    )


def lab_tracker_agent_consultation_policy() -> str:
    return AGENT_CONSULTATION_POLICY


def lab_tracker_code_conventions() -> str:
    return code_facing_idioms()


def register_resources(server: Any) -> None:
    server.resource(
        "lab-tracker://quickstart",
        name="Lab Tracker MCP Quickstart",
        mime_type="text/markdown",
    )(lab_tracker_quickstart)
    server.resource(
        "lab-tracker://surface",
        name="Lab Tracker Retained V1 Surface",
        mime_type="text/markdown",
    )(lab_tracker_surface)
    server.resource(
        "lab-tracker://agent-consultation-policy",
        name="Lab Tracker Agent Consultation Policy",
        mime_type="text/markdown",
    )(lab_tracker_agent_consultation_policy)
    server.resource(
        "lab-tracker://code-conventions",
        name="Lab Tracker Code Conventions",
        mime_type="text/markdown",
    )(lab_tracker_code_conventions)
