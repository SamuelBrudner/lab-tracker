"""Pure operator-owned authority grants for registered data stores.

This module parses one bounded, versioned configuration snapshot and performs
only structural registration authorization.  It deliberately owns no database,
request, credential, cache, filesystem, network, or subprocess dependency.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from itertools import islice
from typing import Final, TypeAlias
from uuid import UUID

from lab_tracker.data_store_definition import (
    DataStoreDefinitionError,
    ValidatedDataStoreDefinition,
)
from lab_tracker.git_remote_policy import GitRemoteAddress, parse_git_remote_address
from lab_tracker.local_filesystem_authority import LocalFilesystemAuthorityBoundary
from lab_tracker.models import (
    StoreCapability,
    StoreKind,
    default_store_capabilities,
)
from lab_tracker.outbound_http import RegisteredHttpPrefix
from lab_tracker.rclone_store_definition import (
    RCLONE_BACKED_STORE_KINDS,
    RegisteredRcloneStoreAddress,
)

STORE_AUTHORITY_CONFIG_SCHEMA: Final = "lab-tracker/store-authority/v1"
STORE_AUTHORITY_FINGERPRINT_PREFIX: Final = "sag-v1-sha256:"
MAX_STORE_AUTHORITY_CONFIG_CHARACTERS: Final = 24 * 1024
MAX_STORE_AUTHORITY_CONFIG_BYTES: Final = 24 * 1024
MAX_STORE_AUTHORITY_GRANTS: Final = 64
MAX_STORE_AUTHORITY_GRANT_ID_LENGTH: Final = 128
MAX_STORE_AUTHORITY_JSON_DEPTH: Final = 8
MAX_STORE_AUTHORITY_JSON_NODES: Final = 1024

_MAX_JSON_OBJECT_KEYS: Final = 8
_MAX_JSON_LIST_ITEMS: Final = 64
_GRANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMON_GRANT_FIELDS: Final = frozenset({"grant_id", "scope", "kind", "root", "capabilities"})
_RCLONE_GRANT_FIELDS: Final = frozenset({"remote", "credential_mode"})
_SUPPORTED_STORE_KINDS: Final = frozenset(
    {StoreKind.LOCAL_FS, StoreKind.HTTP, StoreKind.GIT, *RCLONE_BACKED_STORE_KINDS}
)
_AUTHORITY_DEFINITION_NAME: Final = "store-authority"


class StoreAuthorityRegistryErrorCode(str, Enum):
    """Stable safe categories for operator configuration failures."""

    INVALID_TYPE = "invalid_type"
    TOO_LARGE = "too_large"
    INVALID_TEXT = "invalid_text"
    INVALID_JSON = "invalid_json"
    INVALID_SCHEMA = "invalid_schema"
    INVALID_GRANT = "invalid_grant"
    DUPLICATE_GRANT_ID = "duplicate_grant_id"
    AMBIGUOUS_GRANTS = "ambiguous_grants"


_ERROR_MESSAGES: Final[dict[StoreAuthorityRegistryErrorCode, str]] = {
    StoreAuthorityRegistryErrorCode.INVALID_TYPE: (
        "Store authority grant configuration must be a string."
    ),
    StoreAuthorityRegistryErrorCode.TOO_LARGE: (
        "Store authority grant configuration exceeds its safe limit."
    ),
    StoreAuthorityRegistryErrorCode.INVALID_TEXT: (
        "Store authority grant configuration contains invalid text."
    ),
    StoreAuthorityRegistryErrorCode.INVALID_JSON: (
        "Store authority grant configuration is not valid strict JSON."
    ),
    StoreAuthorityRegistryErrorCode.INVALID_SCHEMA: (
        "Store authority grant configuration has an invalid schema."
    ),
    StoreAuthorityRegistryErrorCode.INVALID_GRANT: (
        "Store authority grant configuration contains an invalid grant."
    ),
    StoreAuthorityRegistryErrorCode.DUPLICATE_GRANT_ID: (
        "Store authority grant configuration contains a duplicate grant identifier."
    ),
    StoreAuthorityRegistryErrorCode.AMBIGUOUS_GRANTS: (
        "Store authority grant configuration contains ambiguous grants."
    ),
}


class StoreAuthorityRegistryError(ValueError):
    """A static configuration failure that never retains rejected values."""

    def __init__(self, code: StoreAuthorityRegistryErrorCode) -> None:
        if not isinstance(code, StoreAuthorityRegistryErrorCode):
            raise TypeError("Store authority registry error code is invalid.")
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


@dataclass(frozen=True, slots=True, repr=False)
class ProjectStoreScope:
    """One exact project authority scope."""

    project_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, UUID):
            raise TypeError("Project store scope is invalid.")


@dataclass(frozen=True, slots=True, repr=False)
class GroupStoreScope:
    """One exact project-group authority scope."""

    group_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.group_id, UUID):
            raise TypeError("Group store scope is invalid.")


StoreAuthorityScope: TypeAlias = ProjectStoreScope | GroupStoreScope


class RcloneCredentialMode(str, Enum):
    """How a registered store selects its exact effective rclone remote."""

    NAME_FALLBACK = "name_fallback"
    CREDENTIAL_REF = "credential_ref"


_STORE_AUTHORITY_PROOF_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False, eq=False, repr=False)
class StoreAuthorityProof:
    """Opaque proof of one exact successful registry authorization."""

    grant_id: str
    fingerprint: str

    def __init__(
        self,
        *,
        grant_id: str,
        fingerprint: str,
        _factory_token: object,
    ) -> None:
        if _factory_token is not _STORE_AUTHORITY_PROOF_FACTORY_TOKEN:
            raise TypeError("StoreAuthorityProof must be built by the registry.")
        object.__setattr__(self, "grant_id", grant_id)
        object.__setattr__(self, "fingerprint", fingerprint)


_STORE_AUTHORITY_BOUNDARY_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False, repr=False)
class HttpStoreAuthorityBoundary:
    """One canonical HTTP origin and ordered directory-prefix boundary."""

    origin: str
    components: tuple[str, ...]

    def __init__(
        self,
        *,
        origin: str,
        components: tuple[str, ...],
        _factory_token: object,
    ) -> None:
        if _factory_token is not _STORE_AUTHORITY_BOUNDARY_FACTORY_TOKEN:
            raise TypeError("HTTP store authority boundaries are registry-owned.")
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "components", components)


@dataclass(frozen=True, slots=True, init=False, repr=False)
class RcloneStoreAuthorityBoundary:
    """One exact effective remote and ordered decoded root boundary."""

    remote: str
    rooted: bool
    components: tuple[str, ...]

    def __init__(
        self,
        *,
        remote: str,
        rooted: bool,
        components: tuple[str, ...],
        _factory_token: object,
    ) -> None:
        if _factory_token is not _STORE_AUTHORITY_BOUNDARY_FACTORY_TOKEN:
            raise TypeError("Rclone store authority boundaries are registry-owned.")
        object.__setattr__(self, "remote", remote)
        object.__setattr__(self, "rooted", rooted)
        object.__setattr__(self, "components", components)


@dataclass(frozen=True, slots=True, init=False, repr=False)
class GitStoreAuthorityBoundary:
    """One full canonical Git remote family and ordered repository prefix."""

    scheme: str
    host: str
    effective_port: int
    ssh_user: str | None
    path_style: str
    host_is_ipv6: bool
    components: tuple[str, ...]

    def __init__(
        self,
        *,
        scheme: str,
        host: str,
        effective_port: int,
        ssh_user: str | None,
        path_style: str,
        host_is_ipv6: bool,
        components: tuple[str, ...],
        _factory_token: object,
    ) -> None:
        if _factory_token is not _STORE_AUTHORITY_BOUNDARY_FACTORY_TOKEN:
            raise TypeError("Git store authority boundaries are registry-owned.")
        object.__setattr__(self, "scheme", scheme)
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "effective_port", effective_port)
        object.__setattr__(self, "ssh_user", ssh_user)
        object.__setattr__(self, "path_style", path_style)
        object.__setattr__(self, "host_is_ipv6", host_is_ipv6)
        object.__setattr__(self, "components", components)


StoreAuthorityBoundary: TypeAlias = (
    LocalFilesystemAuthorityBoundary
    | HttpStoreAuthorityBoundary
    | RcloneStoreAuthorityBoundary
    | GitStoreAuthorityBoundary
)


@dataclass(frozen=True, slots=True, repr=False)
class _StoreAuthorityGrant:
    grant_id: str
    scope: StoreAuthorityScope
    kind: StoreKind
    boundary: StoreAuthorityBoundary
    capabilities: frozenset[StoreCapability]
    credential_mode: RcloneCredentialMode | None
    fingerprint: str
    proof: StoreAuthorityProof


_STORE_AUTHORITY_REGISTRY_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False, eq=False, repr=False)
class StoreAuthorityRegistry:
    """One immutable, deny-by-default operator authority snapshot."""

    _grants: tuple[_StoreAuthorityGrant, ...]

    def __init__(
        self,
        grants: tuple[_StoreAuthorityGrant, ...],
        *,
        _factory_token: object,
    ) -> None:
        if _factory_token is not _STORE_AUTHORITY_REGISTRY_FACTORY_TOKEN:
            raise TypeError("StoreAuthorityRegistry must be built by its factory.")
        if not isinstance(grants, tuple) or not all(
            isinstance(grant, _StoreAuthorityGrant) for grant in grants
        ):
            raise TypeError("Store authority registry grants are invalid.")
        object.__setattr__(self, "_grants", grants)

    @classmethod
    def deny_all(cls) -> StoreAuthorityRegistry:
        """Return one immutable registry containing no grants."""

        return cls((), _factory_token=_STORE_AUTHORITY_REGISTRY_FACTORY_TOKEN)

    @classmethod
    def from_json(cls, raw: object) -> StoreAuthorityRegistry:
        """Parse the bounded operator snapshot without leaking rejected input."""

        grants, error = _parse_registry_config(raw)
        if error is not None:
            # The decoder and every raw-value parser have already returned, so
            # this exception cannot retain a raw-bearing parser exception as
            # ``__context__``.
            raise StoreAuthorityRegistryError(error)
        if grants is None:  # pragma: no cover - guarded by the result invariant
            raise RuntimeError("Store authority registry parser invariant was violated.")
        return cls(
            tuple(sorted(grants, key=lambda grant: grant.grant_id)),
            _factory_token=_STORE_AUTHORITY_REGISTRY_FACTORY_TOKEN,
        )

    @property
    def grant_count(self) -> int:
        """Return the non-sensitive number of configured grants."""

        return len(self._grants)

    def authorize(
        self,
        *,
        grant_id: str,
        scope: StoreAuthorityScope,
        candidate: ValidatedDataStoreDefinition,
        capabilities: Iterable[StoreCapability],
    ) -> StoreAuthorityProof | None:
        """Authorize a non-empty duplicate-free capability request for one grant."""

        if (
            type(grant_id) is not str
            or type(scope) not in (ProjectStoreScope, GroupStoreScope)
            or type(candidate) is not ValidatedDataStoreDefinition
        ):
            return None
        requested = _bounded_capability_set(capabilities)
        if requested is None:
            return None
        grant = next(
            (configured for configured in self._grants if configured.grant_id == grant_id),
            None,
        )
        if (
            grant is None
            or grant.scope != scope
            or grant.kind is not candidate.kind
            or not requested.issubset(grant.capabilities)
        ):
            return None
        boundary, credential_mode = _boundary_for_definition(candidate)
        if (
            boundary is None
            or credential_mode is not grant.credential_mode
            or not _boundary_contains(grant.boundary, boundary)
        ):
            return None
        return grant.proof


class _JSONHookFailure(ValueError):
    """Internal decoder sentinel; never escape the raw parser helper."""


_JSON_DECODE_FAILED = object()


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    decoded: dict[str, object] = {}
    for key, value in pairs:
        if key in decoded:
            raise _JSONHookFailure
        decoded[key] = value
    return decoded


def _reject_json_constant(_value: str) -> object:
    raise _JSONHookFailure


def _decode_json(raw: str) -> object:
    decoded: object | None = None
    failed = False
    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (RecursionError, ValueError):
        failed = True
    return _JSON_DECODE_FAILED if failed else decoded


def _parse_registry_config(
    raw: object,
) -> tuple[list[_StoreAuthorityGrant] | None, StoreAuthorityRegistryErrorCode | None]:
    if raw is None or (type(raw) is str and raw == ""):
        return [], None
    if type(raw) is not str:
        return None, StoreAuthorityRegistryErrorCode.INVALID_TYPE
    if len(raw) > MAX_STORE_AUTHORITY_CONFIG_CHARACTERS:
        return None, StoreAuthorityRegistryErrorCode.TOO_LARGE
    try:
        encoded = raw.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None, StoreAuthorityRegistryErrorCode.INVALID_TEXT
    if len(encoded) > MAX_STORE_AUTHORITY_CONFIG_BYTES:
        return None, StoreAuthorityRegistryErrorCode.TOO_LARGE
    if any(unicodedata.category(character) == "Cc" for character in raw):
        return None, StoreAuthorityRegistryErrorCode.INVALID_TEXT

    decoded = _decode_json(raw)
    if decoded is _JSON_DECODE_FAILED:
        return None, StoreAuthorityRegistryErrorCode.INVALID_JSON
    if not _json_shape_is_bounded(decoded):
        return None, StoreAuthorityRegistryErrorCode.INVALID_SCHEMA
    if type(decoded) is not dict or set(decoded) != {"schema", "grants"}:
        return None, StoreAuthorityRegistryErrorCode.INVALID_SCHEMA
    if decoded.get("schema") != STORE_AUTHORITY_CONFIG_SCHEMA:
        return None, StoreAuthorityRegistryErrorCode.INVALID_SCHEMA
    raw_grants = decoded.get("grants")
    if type(raw_grants) is not list or len(raw_grants) > MAX_STORE_AUTHORITY_GRANTS:
        return None, StoreAuthorityRegistryErrorCode.INVALID_SCHEMA

    grants: list[_StoreAuthorityGrant] = []
    grant_ids: set[str] = set()
    for raw_grant in raw_grants:
        parsed = _parse_grant(raw_grant)
        if parsed is None:
            return None, StoreAuthorityRegistryErrorCode.INVALID_GRANT
        if parsed.grant_id in grant_ids:
            return None, StoreAuthorityRegistryErrorCode.DUPLICATE_GRANT_ID
        if any(_grants_overlap(existing, parsed) for existing in grants):
            return None, StoreAuthorityRegistryErrorCode.AMBIGUOUS_GRANTS
        grant_ids.add(parsed.grant_id)
        grants.append(parsed)
    return grants, None


def _json_shape_is_bounded(value: object) -> bool:
    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > MAX_STORE_AUTHORITY_JSON_DEPTH or nodes > MAX_STORE_AUTHORITY_JSON_NODES:
            return False
        if type(current) is dict:
            if len(current) > _MAX_JSON_OBJECT_KEYS:
                return False
            stack.extend((child, depth + 1) for child in current.values())
        elif type(current) is list:
            if len(current) > _MAX_JSON_LIST_ITEMS:
                return False
            stack.extend((child, depth + 1) for child in current)
    return True


def _parse_grant(raw: object) -> _StoreAuthorityGrant | None:
    if type(raw) is not dict:
        return None
    kind = _parse_store_kind(raw.get("kind"))
    if kind is None or kind not in _SUPPORTED_STORE_KINDS:
        return None
    expected_fields = (
        _COMMON_GRANT_FIELDS | _RCLONE_GRANT_FIELDS
        if kind in RCLONE_BACKED_STORE_KINDS
        else _COMMON_GRANT_FIELDS
    )
    if set(raw) != expected_fields:
        return None

    grant_id = raw.get("grant_id")
    if (
        not isinstance(grant_id, str)
        or len(grant_id) > MAX_STORE_AUTHORITY_GRANT_ID_LENGTH
        or _GRANT_ID.fullmatch(grant_id) is None
    ):
        return None
    scope = _parse_scope(raw.get("scope"))
    capabilities = _parse_configured_capabilities(raw.get("capabilities"), kind=kind)
    if scope is None or capabilities is None:
        return None

    credential_mode: RcloneCredentialMode | None = None
    remote: str | None = None
    if kind in RCLONE_BACKED_STORE_KINDS:
        remote = _parse_rclone_remote(raw.get("remote"))
        credential_mode = _parse_credential_mode(raw.get("credential_mode"))
        if remote is None or credential_mode is None:
            return None

    definition = _definition_for_grant(
        kind=kind,
        root=raw.get("root"),
        remote=remote,
        credential_mode=credential_mode,
    )
    if definition is None:
        return None
    boundary, parsed_mode = _boundary_for_definition(definition)
    if boundary is None or parsed_mode is not credential_mode:
        return None

    fingerprint = _grant_fingerprint(
        scope=scope,
        kind=kind,
        boundary=boundary,
        capabilities=capabilities,
        credential_mode=credential_mode,
    )
    proof = StoreAuthorityProof(
        grant_id=grant_id,
        fingerprint=fingerprint,
        _factory_token=_STORE_AUTHORITY_PROOF_FACTORY_TOKEN,
    )
    return _StoreAuthorityGrant(
        grant_id=grant_id,
        scope=scope,
        kind=kind,
        boundary=boundary,
        capabilities=capabilities,
        credential_mode=credential_mode,
        fingerprint=fingerprint,
        proof=proof,
    )


def _parse_store_kind(value: object) -> StoreKind | None:
    if not isinstance(value, str):
        return None
    parsed: StoreKind | None = None
    with suppress(ValueError):
        parsed = StoreKind(value)
    return parsed


def _parse_scope(value: object) -> StoreAuthorityScope | None:
    if type(value) is not dict or len(value) != 1:
        return None
    if set(value) == {"project_id"}:
        scope_id = _parse_canonical_uuid(value.get("project_id"))
        return ProjectStoreScope(scope_id) if scope_id is not None else None
    if set(value) == {"group_id"}:
        scope_id = _parse_canonical_uuid(value.get("group_id"))
        return GroupStoreScope(scope_id) if scope_id is not None else None
    return None


def _parse_canonical_uuid(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    parsed: UUID | None = None
    with suppress(AttributeError, ValueError):
        parsed = UUID(value)
    if parsed is None or str(parsed) != value:
        return None
    return parsed


def _parse_configured_capabilities(
    value: object,
    *,
    kind: StoreKind,
) -> frozenset[StoreCapability] | None:
    if type(value) is not list or not value or len(value) > len(StoreCapability):
        return None
    parsed: list[StoreCapability] = []
    for item in value:
        capability = _parse_store_capability(item)
        if capability is None or capability in parsed:
            return None
        parsed.append(capability)
    configured = frozenset(parsed)
    supported = frozenset(default_store_capabilities(kind))
    return configured if configured.issubset(supported) else None


def _parse_store_capability(value: object) -> StoreCapability | None:
    if not isinstance(value, str):
        return None
    parsed: StoreCapability | None = None
    with suppress(ValueError):
        parsed = StoreCapability(value)
    return parsed


def _bounded_capability_set(
    capabilities: Iterable[StoreCapability],
) -> frozenset[StoreCapability] | None:
    try:
        values = tuple(islice(iter(capabilities), len(StoreCapability) + 1))
    except Exception:
        # Authorization is a total, opaque decision boundary. A caller-owned
        # iterable that cannot yield its typed request is simply not authorized.
        return None
    if not values or len(values) > len(StoreCapability):
        return None
    if any(not isinstance(value, StoreCapability) for value in values):
        return None
    unique = frozenset(values)
    return unique if len(unique) == len(values) else None


def _parse_credential_mode(value: object) -> RcloneCredentialMode | None:
    if not isinstance(value, str):
        return None
    parsed: RcloneCredentialMode | None = None
    with suppress(ValueError):
        parsed = RcloneCredentialMode(value)
    return parsed


def _parse_rclone_remote(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    # Reuse the authoritative rclone address parser through construction below.
    return value


def _definition_for_grant(
    *,
    kind: StoreKind,
    root: object,
    remote: str | None,
    credential_mode: RcloneCredentialMode | None,
) -> ValidatedDataStoreDefinition | None:
    name: object = _AUTHORITY_DEFINITION_NAME
    credential_ref: object = None
    if kind in RCLONE_BACKED_STORE_KINDS:
        if remote is None or credential_mode is None:
            return None
        if credential_mode is RcloneCredentialMode.NAME_FALLBACK:
            name = remote
        else:
            credential_ref = remote
    parsed: ValidatedDataStoreDefinition | None = None
    with suppress(DataStoreDefinitionError):
        parsed = ValidatedDataStoreDefinition.create(
            name=name,
            kind=kind,
            root=root,
            credential_ref=credential_ref,
        )
    return parsed


def _boundary_for_definition(
    definition: ValidatedDataStoreDefinition,
) -> tuple[StoreAuthorityBoundary | None, RcloneCredentialMode | None]:
    if definition.kind is StoreKind.LOCAL_FS:
        return LocalFilesystemAuthorityBoundary.parse(definition.root), None
    if definition.kind is StoreKind.HTTP:
        prefix = RegisteredHttpPrefix.parse(definition.root)
        return (
            (
                HttpStoreAuthorityBoundary(
                    origin=prefix.origin,
                    components=prefix.path_components,
                    _factory_token=_STORE_AUTHORITY_BOUNDARY_FACTORY_TOKEN,
                )
                if prefix is not None
                else None
            ),
            None,
        )
    if definition.kind is StoreKind.GIT:
        remote = parse_git_remote_address(definition.root)
        return (
            (_git_authority_boundary(remote) if remote is not None else None),
            None,
        )
    if definition.kind in RCLONE_BACKED_STORE_KINDS:
        address = RegisteredRcloneStoreAddress.parse(
            kind=definition.kind,
            name=definition.name,
            root=definition.root,
            credential_ref=definition.credential_ref,
        )
        mode = (
            RcloneCredentialMode.NAME_FALLBACK
            if definition.credential_ref is None
            else RcloneCredentialMode.CREDENTIAL_REF
        )
        return (
            (
                RcloneStoreAuthorityBoundary(
                    remote=address.remote.value,
                    rooted=address.root.rooted,
                    components=address.root.components,
                    _factory_token=_STORE_AUTHORITY_BOUNDARY_FACTORY_TOKEN,
                )
                if address is not None
                else None
            ),
            mode,
        )
    return None, None


def _git_authority_boundary(remote: GitRemoteAddress) -> GitStoreAuthorityBoundary:
    return GitStoreAuthorityBoundary(
        scheme=remote.scheme,
        host=remote.host,
        effective_port=remote.effective_port,
        ssh_user=remote.ssh_user,
        path_style=remote.path_style.value,
        host_is_ipv6=remote.host_is_ipv6,
        components=remote.path_segments,
        _factory_token=_STORE_AUTHORITY_BOUNDARY_FACTORY_TOKEN,
    )


def _boundary_contains(
    configured: StoreAuthorityBoundary,
    candidate: StoreAuthorityBoundary,
) -> bool:
    if isinstance(configured, LocalFilesystemAuthorityBoundary):
        return isinstance(candidate, LocalFilesystemAuthorityBoundary) and configured.contains(
            candidate
        )
    if isinstance(configured, HttpStoreAuthorityBoundary):
        return (
            isinstance(candidate, HttpStoreAuthorityBoundary)
            and candidate.origin == configured.origin
            and _components_contain(configured.components, candidate.components)
        )
    if isinstance(configured, RcloneStoreAuthorityBoundary):
        return (
            isinstance(candidate, RcloneStoreAuthorityBoundary)
            and candidate.remote == configured.remote
            and candidate.rooted is configured.rooted
            and _components_contain(configured.components, candidate.components)
        )
    if isinstance(configured, GitStoreAuthorityBoundary):
        return (
            isinstance(candidate, GitStoreAuthorityBoundary)
            and _git_family(configured) == _git_family(candidate)
            and _components_contain(configured.components, candidate.components)
        )
    return False


def _components_contain(
    configured: Sequence[str],
    candidate: Sequence[str],
) -> bool:
    size = len(configured)
    return len(candidate) >= size and tuple(candidate[:size]) == tuple(configured)


def _git_family(address: GitStoreAuthorityBoundary) -> tuple[object, ...]:
    return (
        address.scheme,
        address.host,
        address.effective_port,
        address.ssh_user,
        address.path_style,
        address.host_is_ipv6,
    )


def _grants_overlap(
    first: _StoreAuthorityGrant,
    second: _StoreAuthorityGrant,
) -> bool:
    if first.scope != second.scope or first.kind is not second.kind:
        return False
    return _boundaries_overlap(first.boundary, second.boundary)


def _boundaries_overlap(
    first: StoreAuthorityBoundary,
    second: StoreAuthorityBoundary,
) -> bool:
    return _boundary_contains(first, second) or _boundary_contains(second, first)


def _grant_fingerprint(
    *,
    scope: StoreAuthorityScope,
    kind: StoreKind,
    boundary: StoreAuthorityBoundary,
    capabilities: frozenset[StoreCapability],
    credential_mode: RcloneCredentialMode | None,
) -> str:
    payload = {
        "boundary": _boundary_fingerprint_payload(boundary, credential_mode),
        "capabilities": sorted(capability.value for capability in capabilities),
        "domain": "lab-tracker/store-authority-grant",
        "kind": kind.value,
        "scope": _scope_fingerprint_payload(scope),
        "version": 1,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return f"{STORE_AUTHORITY_FINGERPRINT_PREFIX}{hashlib.sha256(encoded).hexdigest()}"


def _scope_fingerprint_payload(scope: StoreAuthorityScope) -> dict[str, str]:
    if isinstance(scope, ProjectStoreScope):
        return {"id": str(scope.project_id), "type": "project"}
    return {"id": str(scope.group_id), "type": "group"}


def _boundary_fingerprint_payload(
    boundary: StoreAuthorityBoundary,
    credential_mode: RcloneCredentialMode | None,
) -> dict[str, object]:
    if isinstance(boundary, LocalFilesystemAuthorityBoundary):
        return {
            "anchor": boundary.anchor,
            "components": list(boundary.components),
            "flavor": boundary.flavor,
            "type": "local",
        }
    if isinstance(boundary, HttpStoreAuthorityBoundary):
        return {
            "components": list(boundary.components),
            "origin": boundary.origin,
            "type": "http",
        }
    if isinstance(boundary, RcloneStoreAuthorityBoundary):
        if credential_mode is None:  # pragma: no cover - guarded by grant construction
            raise RuntimeError("Rclone credential mode invariant was violated.")
        return {
            "components": list(boundary.components),
            "credential_mode": credential_mode.value,
            "remote": boundary.remote,
            "rooted": boundary.rooted,
            "type": "rclone",
        }
    return {
        "components": list(boundary.components),
        "effective_port": boundary.effective_port,
        "host": boundary.host,
        "host_is_ipv6": boundary.host_is_ipv6,
        "path_style": boundary.path_style,
        "scheme": boundary.scheme,
        "ssh_user": boundary.ssh_user,
        "type": "git",
    }
