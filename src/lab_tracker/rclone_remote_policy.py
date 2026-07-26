"""Exact authorization for rclone remotes used by server-side subprocesses.

Rclone remote names are opaque, case-sensitive configuration identifiers.  The
policy therefore validates their grammar but never trims, case-folds, or
normalizes their spelling before comparing a candidate with an operator grant.
Only an authorized :class:`RcloneRemoteName` should be composed into argv.
"""

from __future__ import annotations

from dataclasses import dataclass

from lab_tracker.rclone_store_locator import RcloneRemoteName

DEFAULT_RCLONE_REMOTE_POLICY_VARIABLE = "LAB_TRACKER_RCLONE_ALLOWED_REMOTES"


@dataclass(frozen=True, slots=True)
class RcloneRemotePolicy:
    """Immutable allowlist of exact, structurally validated rclone names."""

    _grants: tuple[RcloneRemoteName, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self._grants, tuple):
            raise ValueError("Rclone remote policy is invalid.")
        if any(not isinstance(grant, RcloneRemoteName) for grant in self._grants):
            raise ValueError("Rclone remote policy is invalid.")
        if len(set(self._grants)) != len(self._grants):
            raise ValueError("Rclone remote policy is invalid.")

    @classmethod
    def deny_all(cls) -> RcloneRemotePolicy:
        """Return a policy with no authorized remotes."""

        return cls()

    @classmethod
    def from_config(
        cls,
        raw: str | None,
        *,
        variable: str = DEFAULT_RCLONE_REMOTE_POLICY_VARIABLE,
    ) -> RcloneRemotePolicy:
        """Parse exact comma-separated grants without echoing configured values.

        Entries are deliberately not stripped.  Invalid and duplicate entries
        identify only the setting, their one-based index, and a safe category.
        """

        if raw is None or raw == "":
            return cls.deny_all()

        grants: list[RcloneRemoteName] = []
        seen: set[RcloneRemoteName] = set()
        for index, entry in enumerate(raw.split(","), start=1):
            if not entry:
                raise _config_error(variable, index, "empty entry")
            grant = RcloneRemoteName.parse(entry)
            if grant is None:
                raise _config_error(variable, index, "invalid remote name")
            if grant in seen:
                raise _config_error(variable, index, "duplicate exact grant")
            seen.add(grant)
            grants.append(grant)
        return cls(tuple(grants))

    def authorize(self, candidate: str) -> RcloneRemoteName | None:
        """Return an exactly authorized typed name, or ``None``."""

        remote = RcloneRemoteName.parse(candidate)
        if remote is None:
            return None
        return self.authorize_name(remote)

    def authorize_name(
        self,
        remote: RcloneRemoteName,
    ) -> RcloneRemoteName | None:
        """Authorize an already validated name without changing its spelling."""

        if not isinstance(remote, RcloneRemoteName):
            return None
        if remote in self._grants:
            return remote
        return None


def _config_error(variable: str, index: int, category: str) -> ValueError:
    return ValueError(f"{variable} entry {index} is invalid ({category}).")
