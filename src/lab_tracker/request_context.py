"""Request-scoped repository and deferred side effects."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from lab_tracker.repository import LabTrackerRepository


@dataclass
class LabTrackerRequestContext:
    repository: LabTrackerRepository
    surface: str | None = None
    after_commit_actions: list[Callable[[], None]] = field(default_factory=list)
    after_rollback_actions: list[Callable[[], None]] = field(default_factory=list)

    def complete_commit(
        self,
        *,
        run_deferred_actions: Callable[[list[Callable[[], None]] | None, str], None],
    ) -> None:
        # A post-commit reconciliation may schedule its own fail-soft usage
        # event. Drain in generations so those newly appended actions run in
        # this completed request rather than being cleared unseen.
        while self.after_commit_actions:
            actions = list(self.after_commit_actions)
            self.after_commit_actions.clear()
            run_deferred_actions(actions, "after_commit")
        self.after_rollback_actions.clear()

    def complete_rollback(
        self,
        *,
        run_deferred_actions: Callable[[list[Callable[[], None]] | None, str], None],
    ) -> None:
        run_deferred_actions(self.after_rollback_actions, "after_rollback")
        self.after_commit_actions.clear()
        self.after_rollback_actions.clear()
