"""The deterministic provenance stage every claimed batch execution runs once.

Three rule-based detectors propose human-gated :class:`ProvenanceLink` rows
before the model drafts: the content-hash detector (two captures share
bytes), the exact-id detector (a capture's own metadata names one session
or committed analysis), and the time-window detector (a capture that names
no session was made inside exactly one session window). Each is best effort:
a failure in one is logged and swallowed so it can never flip the LLM batch
to FAILED, block drafting, or stop the other detectors from running.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.services.graph_draft_scheduling_ports import SchedulingProvenanceLinks

logger = logging.getLogger(__name__)

Detector = Callable[..., int]


def _detectors(links: SchedulingProvenanceLinks) -> tuple[tuple[str, Detector], ...]:
    return (
        ("content-hash", links.propose_links_from_content_hash),
        ("exact-id", links.propose_links_from_id_matches),
        ("time-window", links.propose_links_from_time_windows),
    )


def propose_deterministic_links(
    links: SchedulingProvenanceLinks,
    project_id: UUID,
    *,
    actor: AuthContext | None,
) -> None:
    """Run every deterministic detector for ``project_id``, each in isolation."""

    for name, detector in _detectors(links):
        try:
            detector(project_id, actor=actor)
        except Exception:
            logger.exception(
                "%s provenance-link detector failed for project %s", name, project_id
            )


__all__ = ["propose_deterministic_links"]
