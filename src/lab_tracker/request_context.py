"""Explicit request-scoped persistence and deferred action context."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from lab_tracker.repository import LabTrackerRepository


@dataclass
class LabTrackerRequestContext:
    repository: LabTrackerRepository
    active_store: object | None = None
    pending_search_ops: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)
    after_commit_actions: list[Callable[[], None]] = field(default_factory=list)
    after_rollback_actions: list[Callable[[], None]] = field(default_factory=list)

    def finish(
        self,
        *,
        committed: bool,
        apply_search_op: Callable[[str, tuple[object, ...]], None],
        run_deferred_actions: Callable[[list[Callable[[], None]] | None, str], None],
    ) -> None:
        if committed:
            for operation, args in self.pending_search_ops:
                apply_search_op(operation, args)
            run_deferred_actions(self.after_commit_actions, "after_commit")
        else:
            run_deferred_actions(self.after_rollback_actions, "after_rollback")
        self.pending_search_ops.clear()
        self.after_commit_actions.clear()
        self.after_rollback_actions.clear()
        self.active_store = None

    def reset_after_failure(self) -> None:
        self.active_store = None
        self.pending_search_ops.clear()

