"""Evidence-bundle command delegation for :class:`LabTrackerAPI`."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from uuid import UUID

from lab_tracker.api_parts._base import _elapsed_ms
from lab_tracker.auth import AuthContext
from lab_tracker.models import UsageEventOutcome, UsageEventResourceType, UsageEventVerb
from lab_tracker.services.evidence_bundle_service import (
    EvidenceBundleResult,
    RecordEvidenceBundleCommand,
)

if TYPE_CHECKING:
    from lab_tracker.services.evidence_bundle_service import EvidenceBundleService


class EvidenceBundlesApiMixin:
    if TYPE_CHECKING:
        evidence_bundles: EvidenceBundleService

        def record_usage_event(
            self,
            *,
            verb: UsageEventVerb,
            resource_type: UsageEventResourceType,
            project_id: UUID | None = None,
            actor: AuthContext | None = None,
            outcome: UsageEventOutcome = UsageEventOutcome.OK,
            duration_ms: int | None = None,
        ) -> None: ...

    def record_evidence_bundle(
        self,
        command: RecordEvidenceBundleCommand,
        *,
        actor: AuthContext,
    ) -> EvidenceBundleResult:
        # Not routed through ``_with_usage_event``: the service must own the
        # outer application transaction (it rolls back a key-race loser in
        # full), so the usage event is recorded around it, once per call.
        verb = UsageEventVerb.VIEW if command.dry_run else UsageEventVerb.CREATE
        start = time.perf_counter()
        try:
            result = self.evidence_bundles.record(command, actor=actor)
        except Exception:
            self.record_usage_event(
                verb=verb,
                resource_type=UsageEventResourceType.EVIDENCE_BUNDLE,
                project_id=command.project_id,
                actor=actor,
                outcome=UsageEventOutcome.ERROR,
                duration_ms=_elapsed_ms(start),
            )
            raise
        self.record_usage_event(
            verb=verb,
            resource_type=UsageEventResourceType.EVIDENCE_BUNDLE,
            project_id=command.project_id,
            actor=actor,
            duration_ms=_elapsed_ms(start),
        )
        return result
