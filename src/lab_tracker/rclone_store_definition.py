"""Shared classification and address parsing for registered rclone stores."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from lab_tracker.models import StoreKind
from lab_tracker.rclone_store_locator import (
    RcloneRemoteName,
    RegisteredRcloneRoot,
)

RCLONE_BACKED_STORE_KINDS: Final = frozenset(
    {
        StoreKind.SSH,
        StoreKind.S3,
        StoreKind.GCS,
        StoreKind.AZURE_BLOB,
        StoreKind.DROPBOX,
        StoreKind.GDRIVE,
        StoreKind.BOX,
        StoreKind.ONEDRIVE,
        StoreKind.RCLONE,
    }
)


def is_rclone_store_kind(kind: object) -> bool:
    """Return whether a registered store kind uses the rclone adapter."""

    return kind in RCLONE_BACKED_STORE_KINDS


@dataclass(frozen=True, slots=True)
class RegisteredRcloneStoreAddress:
    """One structurally valid registered rclone remote and root."""

    remote: RcloneRemoteName
    root: RegisteredRcloneRoot

    def __post_init__(self) -> None:
        if not isinstance(self.remote, RcloneRemoteName) or not isinstance(
            self.root,
            RegisteredRcloneRoot,
        ):
            raise ValueError("Registered rclone store address is invalid.")

    @classmethod
    def parse(
        cls,
        *,
        kind: object,
        name: object,
        root: object,
        credential_ref: object,
    ) -> RegisteredRcloneStoreAddress | None:
        """Parse store fields under the one authoritative rclone interpretation.

        A non-``None`` credential reference always selects the remote, including
        when blank or malformed; only absence falls back to the registered name.
        """

        if not is_rclone_store_kind(kind):
            return None
        remote_value = name if credential_ref is None else credential_ref
        remote = RcloneRemoteName.parse(remote_value)
        registered_root = RegisteredRcloneRoot.parse_decoded(root)
        if remote is None or registered_root is None:
            return None
        return cls(remote=remote, root=registered_root)
