"""Pure validation for newly registered data-store definitions.

Repository hydration remains deliberately permissive so legacy rows can still
be inspected and repaired.  This module is the stricter construction boundary
for new definitions: it validates backend-specific syntax, canonicalizes only
through the backend's existing parser, and performs no I/O.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Final, NoReturn

from lab_tracker.git_remote_policy import parse_git_remote_address
from lab_tracker.local_path_policy import is_supported_absolute_local_root
from lab_tracker.local_store_locator import (
    is_valid_local_store_name,
    is_valid_store_name,
)
from lab_tracker.models import StoreKind
from lab_tracker.outbound_http import RegisteredHttpPrefix
from lab_tracker.rclone_store_definition import (
    RegisteredRcloneStoreAddress,
    is_rclone_store_kind,
)
from lab_tracker.rclone_store_locator import RegisteredRcloneRoot

DATA_STORE_NAME_MAX_LENGTH: Final = 255
DATA_STORE_ROOT_MAX_LENGTH: Final = 2000
DATA_STORE_ENDPOINT_MAX_LENGTH: Final = 2000
DATA_STORE_CREDENTIAL_REF_MAX_LENGTH: Final = 255

_SUPPORTED_STORE_KINDS: Final = frozenset(
    {
        StoreKind.LOCAL_FS,
        StoreKind.HTTP,
        StoreKind.GIT,
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


class DataStoreDefinitionErrorCode(str, Enum):
    """Stable machine-readable failures for new data-store definitions."""

    INVALID_KIND = "invalid_kind"
    UNSUPPORTED_KIND = "unsupported_kind"
    INVALID_NAME = "invalid_name"
    NAME_TOO_LONG = "name_too_long"
    INVALID_ROOT = "invalid_root"
    ROOT_TOO_LONG = "root_too_long"
    ENDPOINT_NOT_ALLOWED = "endpoint_not_allowed"
    CREDENTIAL_REF_NOT_ALLOWED = "credential_ref_not_allowed"
    INVALID_CREDENTIAL_REF = "invalid_credential_ref"
    CREDENTIAL_REF_TOO_LONG = "credential_ref_too_long"


_ERROR_MESSAGES: Final[dict[DataStoreDefinitionErrorCode, str]] = {
    DataStoreDefinitionErrorCode.INVALID_KIND: "Data store kind is invalid.",
    DataStoreDefinitionErrorCode.UNSUPPORTED_KIND: "Data store kind is not supported.",
    DataStoreDefinitionErrorCode.INVALID_NAME: "Data store name is invalid.",
    DataStoreDefinitionErrorCode.NAME_TOO_LONG: "Data store name is too long.",
    DataStoreDefinitionErrorCode.INVALID_ROOT: "Data store root is invalid.",
    DataStoreDefinitionErrorCode.ROOT_TOO_LONG: "Data store root is too long.",
    DataStoreDefinitionErrorCode.ENDPOINT_NOT_ALLOWED: "Data store endpoint is not allowed.",
    DataStoreDefinitionErrorCode.CREDENTIAL_REF_NOT_ALLOWED: (
        "Credential reference is not allowed for this data store kind."
    ),
    DataStoreDefinitionErrorCode.INVALID_CREDENTIAL_REF: (
        "Data store credential reference is invalid."
    ),
    DataStoreDefinitionErrorCode.CREDENTIAL_REF_TOO_LONG: (
        "Data store credential reference is too long."
    ),
}


class DataStoreDefinitionError(ValueError):
    """A safe validation failure that never includes request-supplied values."""

    def __init__(self, code: DataStoreDefinitionErrorCode) -> None:
        if not isinstance(code, DataStoreDefinitionErrorCode):
            raise TypeError("Data store definition error code is invalid.")
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


def _reject(code: DataStoreDefinitionErrorCode) -> NoReturn:
    raise DataStoreDefinitionError(code)


def _is_storage_safe_text(value: str) -> bool:
    """Reject text that cannot round-trip through the database text encoding."""

    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return not any(unicodedata.category(character) == "Cc" for character in value)


def _validate_name(name: object, *, local: bool) -> str:
    if not isinstance(name, str) or not name:
        _reject(DataStoreDefinitionErrorCode.INVALID_NAME)
    if len(name) > DATA_STORE_NAME_MAX_LENGTH:
        _reject(DataStoreDefinitionErrorCode.NAME_TOO_LONG)
    if not _is_storage_safe_text(name) or name != name.strip():
        _reject(DataStoreDefinitionErrorCode.INVALID_NAME)
    validator = is_valid_local_store_name if local else is_valid_store_name
    if not validator(name):
        _reject(DataStoreDefinitionErrorCode.INVALID_NAME)
    return name


def _validate_root_input(root: object) -> str:
    if not isinstance(root, str) or not root:
        _reject(DataStoreDefinitionErrorCode.INVALID_ROOT)
    if len(root) > DATA_STORE_ROOT_MAX_LENGTH:
        _reject(DataStoreDefinitionErrorCode.ROOT_TOO_LONG)
    if not _is_storage_safe_text(root):
        _reject(DataStoreDefinitionErrorCode.INVALID_ROOT)
    return root


def _validate_canonical_root(root: str) -> str:
    if len(root) > DATA_STORE_ROOT_MAX_LENGTH:
        _reject(DataStoreDefinitionErrorCode.ROOT_TOO_LONG)
    if not _is_storage_safe_text(root):
        _reject(DataStoreDefinitionErrorCode.INVALID_ROOT)
    return root


def _validate_credential_ref_input(credential_ref: object) -> str | None:
    if credential_ref is None:
        return None
    if not isinstance(credential_ref, str) or not credential_ref:
        _reject(DataStoreDefinitionErrorCode.INVALID_CREDENTIAL_REF)
    if len(credential_ref) > DATA_STORE_CREDENTIAL_REF_MAX_LENGTH:
        _reject(DataStoreDefinitionErrorCode.CREDENTIAL_REF_TOO_LONG)
    if not _is_storage_safe_text(credential_ref):
        _reject(DataStoreDefinitionErrorCode.INVALID_CREDENTIAL_REF)
    return credential_ref


def _render_rclone_root(root: RegisteredRcloneRoot) -> str:
    path = "/".join(root.components)
    return f"/{path}" if root.rooted else path


_DATA_STORE_DEFINITION_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class ValidatedDataStoreDefinition:
    """An immutable, canonical definition proven valid for new registration."""

    name: str
    kind: StoreKind
    root: str
    endpoint: None
    credential_ref: str | None

    def __init__(
        self,
        name: str,
        kind: StoreKind,
        root: str,
        endpoint: None,
        credential_ref: str | None,
        *,
        _factory_token: object,
    ) -> None:
        if _factory_token is not _DATA_STORE_DEFINITION_FACTORY_TOKEN:
            raise TypeError(
                "ValidatedDataStoreDefinition must be built by its validated factory."
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "credential_ref", credential_ref)

    @classmethod
    def create(
        cls,
        *,
        name: object,
        kind: object,
        root: object,
        endpoint: object = None,
        credential_ref: object = None,
    ) -> ValidatedDataStoreDefinition:
        """Validate and canonicalize a new definition without performing I/O."""

        if not isinstance(kind, StoreKind):
            _reject(DataStoreDefinitionErrorCode.INVALID_KIND)
        if kind not in _SUPPORTED_STORE_KINDS:
            _reject(DataStoreDefinitionErrorCode.UNSUPPORTED_KIND)

        validated_name = _validate_name(name, local=kind is StoreKind.LOCAL_FS)
        if endpoint is not None:
            _reject(DataStoreDefinitionErrorCode.ENDPOINT_NOT_ALLOWED)
        validated_root = _validate_root_input(root)

        if kind is StoreKind.LOCAL_FS:
            if credential_ref is not None:
                _reject(DataStoreDefinitionErrorCode.CREDENTIAL_REF_NOT_ALLOWED)
            if not is_supported_absolute_local_root(validated_root):
                _reject(DataStoreDefinitionErrorCode.INVALID_ROOT)
            canonical_root = validated_root
            validated_credential_ref = None
        elif kind is StoreKind.HTTP:
            if credential_ref is not None:
                _reject(DataStoreDefinitionErrorCode.CREDENTIAL_REF_NOT_ALLOWED)
            prefix = RegisteredHttpPrefix.parse(validated_root)
            if prefix is None:
                _reject(DataStoreDefinitionErrorCode.INVALID_ROOT)
            canonical_root = prefix.canonical_url
            validated_credential_ref = None
        elif kind is StoreKind.GIT:
            if credential_ref is not None:
                _reject(DataStoreDefinitionErrorCode.CREDENTIAL_REF_NOT_ALLOWED)
            remote = parse_git_remote_address(validated_root)
            if remote is None:
                _reject(DataStoreDefinitionErrorCode.INVALID_ROOT)
            canonical_root = remote.subprocess_value
            validated_credential_ref = None
        elif is_rclone_store_kind(kind):
            validated_credential_ref = _validate_credential_ref_input(credential_ref)
            address = RegisteredRcloneStoreAddress.parse(
                kind=kind,
                name=validated_name,
                root=validated_root,
                credential_ref=validated_credential_ref,
            )
            if address is None:
                if RegisteredRcloneRoot.parse_decoded(validated_root) is None:
                    _reject(DataStoreDefinitionErrorCode.INVALID_ROOT)
                if validated_credential_ref is not None:
                    _reject(DataStoreDefinitionErrorCode.INVALID_CREDENTIAL_REF)
                _reject(DataStoreDefinitionErrorCode.INVALID_NAME)
            canonical_root = _render_rclone_root(address.root)
        else:  # pragma: no cover - _SUPPORTED_STORE_KINDS is exhaustive
            _reject(DataStoreDefinitionErrorCode.UNSUPPORTED_KIND)

        return cls(
            name=validated_name,
            kind=kind,
            root=_validate_canonical_root(canonical_root),
            endpoint=None,
            credential_ref=validated_credential_ref,
            _factory_token=_DATA_STORE_DEFINITION_FACTORY_TOKEN,
        )
