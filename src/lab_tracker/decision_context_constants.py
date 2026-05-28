"""Constants for assistant decision-context assembly."""

from __future__ import annotations

TASK_KIND_VALUES = (
    "plot",
    "analysis",
    "slides",
    "experiment_plan",
    "summary",
    "research_writing",
)
TASK_KIND_TEXT = ", ".join(TASK_KIND_VALUES)
CONTEXT_LOOKUP_LIMIT = 500

AGENT_CONSULTATION_POLICY = """# Lab Tracker Agent Consultation Policy

Before research-facing decisions, consult the Lab Tracker MCP server. This
includes choosing variables to plot, analyses to run, figures or slides to make,
experimental controls to prioritize, summaries to write, and research writing
such as manuscripts, grants, abstracts, results, discussion text, and figure
legends.

Prefer `lab_tracker_get_decision_context` when available. Otherwise use
`lab_tracker_list_projects`, `lab_tracker_search`, `lab_tracker_list_questions`,
`lab_tracker_list_notes`, and the low-level dataset, analysis, claim, and
visualization read tools.

If Lab Tracker is unavailable or ambiguous, state that explicitly. Do not create
or mutate Lab Tracker records unless the user explicitly asks.
"""
