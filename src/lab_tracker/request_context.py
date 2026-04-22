"""Request-scoped repository and deferred side effects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from lab_tracker.repository import LabTrackerRepository


@dataclass
class LabTrackerRequestContext:
    repository: LabTrackerRepository
    after_commit_actions: list[Callable[[], None]] = field(default_factory=list)
    after_rollback_actions: list[Callable[[], None]] = field(default_factory=list)

    def finish(
        self,
        *,
        committed: bool,
        run_deferred_actions: Callable[[list[Callable[[], None]] | None, str], None],
    ) -> None:
        if committed:
            run_deferred_actions(self.after_commit_actions, "after_commit")
        else:
            run_deferred_actions(self.after_rollback_actions, "after_rollback")
        self.after_commit_actions.clear()
        self.after_rollback_actions.clear()
