"""Per-domain API delegation mixins."""

from __future__ import annotations

from lab_tracker.api_parts.analyses import AnalysesApiMixin
from lab_tracker.api_parts.datasets import DatasetsApiMixin
from lab_tracker.api_parts.exploration import ExplorationApiMixin
from lab_tracker.api_parts.goals import GoalsApiMixin
from lab_tracker.api_parts.graph_drafts import GraphDraftsApiMixin
from lab_tracker.api_parts.notes import NotesApiMixin
from lab_tracker.api_parts.projects import ProjectsApiMixin
from lab_tracker.api_parts.provenance import ProvenanceApiMixin
from lab_tracker.api_parts.questions import QuestionsApiMixin
from lab_tracker.api_parts.sessions import SessionsApiMixin
from lab_tracker.api_parts.usage import UsageApiMixin

__all__ = [
    "ProjectsApiMixin",
    "QuestionsApiMixin",
    "NotesApiMixin",
    "DatasetsApiMixin",
    "SessionsApiMixin",
    "AnalysesApiMixin",
    "GoalsApiMixin",
    "GraphDraftsApiMixin",
    "ExplorationApiMixin",
    "ProvenanceApiMixin",
    "UsageApiMixin",
]
