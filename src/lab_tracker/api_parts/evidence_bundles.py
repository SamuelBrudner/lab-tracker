"""Evidence-bundle command delegation for :class:`LabTrackerAPI`."""

from __future__ import annotations

from typing import Any


class EvidenceBundlesApiMixin:
    def record_evidence_bundle(self, *args: Any, **kwargs: Any) -> Any:
        return self.evidence_bundles.record(*args, **kwargs)
