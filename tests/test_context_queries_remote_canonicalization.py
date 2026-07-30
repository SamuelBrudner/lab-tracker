from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError, dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from store_authority_fakes import (
    ExplodingSnapshotProvider,
    RecordingSnapshotProvider,
    bound_data_store,
)

from lab_tracker.application.context_queries import (
    ContextQueries,
    PreparedExternalArtifactResolution,
)
from lab_tracker.artifact_resolution import (
    GitStoreResolutionTarget,
    HttpStoreResolutionTarget,
    LocalStoreResolutionTarget,
    PreparedArtifactResolutionTarget,
    RcloneStoreResolutionTarget,
    ResolutionStatus,
    ResolvedArtifact,
)
from lab_tracker.auth import AuthContext, Role
from lab_tracker.models import (
    DataStore,
    ExternalArtifactReference,
    StoreCapability,
    StoreKind,
)
from lab_tracker.store_authority_registry import StoreAuthorityRegistry


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


class _ContextApi:
    def __init__(self, *, project_id: UUID, reference: ExternalArtifactReference) -> None:
        self.project_id = project_id
        self.reference = reference
        self.calls: list[str] = []

    def get_dataset_for_read(self, _dataset_id: UUID, *, actor: AuthContext):
        self.calls.append(f"authorized:{actor.user_id}")
        return SimpleNamespace(
            project_id=self.project_id,
            commit_manifest=SimpleNamespace(external_artifacts=[self.reference]),
        )


class _DataStoreLookup:
    def __init__(self, store: DataStore | None = None) -> None:
        self.store = store
        self.names: list[str] = []

    def get_by_name(self, _project_id: UUID, name: str):
        self.names.append(name)
        return self.store


class _ExactDataStoreLookup:
    def __init__(self, *stores: DataStore) -> None:
        self.stores = {store.name: store for store in stores}
        self.names: list[str] = []

    def get_by_name(self, _project_id: UUID, name: str):
        self.names.append(name)
        return self.stores.get(name)


class _ContextRepository:
    def __init__(self, lookup: _DataStoreLookup | _ExactDataStoreLookup) -> None:
        self.data_stores = lookup


class _ResolverRegistry:
    """A resolver seam that records every prepared target it is handed."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.prepared_targets: list[PreparedArtifactResolutionTarget] = []
        self.parameters: list[tuple[int, tuple[int, int] | None]] = []
        self.references: list[ExternalArtifactReference] = []
        self.local_targets: list[LocalStoreResolutionTarget] = []
        self.http_targets: list[HttpStoreResolutionTarget] = []
        self.rclone_targets: list[RcloneStoreResolutionTarget] = []
        self.git_targets: list[GitStoreResolutionTarget] = []

    def resolve_prepared(
        self,
        target: PreparedArtifactResolutionTarget,
        *,
        max_bytes: int,
        byte_range: tuple[int, int] | None,
    ) -> ResolvedArtifact:
        self.events.append("resolve-prepared")
        self.prepared_targets.append(target)
        self.parameters.append((max_bytes, byte_range))
        if isinstance(target, ExternalArtifactReference):
            self.references.append(target)
            reference = target
        elif isinstance(target, LocalStoreResolutionTarget):
            self.local_targets.append(target)
            reference = target.logical_reference
        elif isinstance(target, HttpStoreResolutionTarget):
            self.http_targets.append(target)
            reference = target.logical_reference
        elif isinstance(target, RcloneStoreResolutionTarget):
            self.rclone_targets.append(target)
            reference = target.logical_reference
        elif isinstance(target, GitStoreResolutionTarget):
            self.git_targets.append(target)
            reference = target.logical_reference
        else:
            raise AssertionError("unsupported prepared target reached test registry")
        full_content = b"ok"
        if byte_range is None:
            content = full_content[:max_bytes]
        else:
            start, end = byte_range
            content = full_content[start : min(end, start + max_bytes)]
        return ResolvedArtifact(
            status=ResolutionStatus.VERIFIED,
            source_system=reference.source_system,
            uri=reference.uri,
            expected_hash=reference.content_hash,
            observed_hash=reference.content_hash,
            size_bytes=len(full_content),
            content=content,
            truncated=len(content) < len(full_content),
            fetched_at=datetime.now(timezone.utc),
        )


@dataclass(frozen=True)
class _StoreResolution:
    """Everything one prepare-then-resolve round trip made observable."""

    prepared: PreparedExternalArtifactResolution
    result: dict[str, object]
    events: list[str]
    resolver: _ResolverRegistry
    lookup: _DataStoreLookup | _ExactDataStoreLookup


def _resolve_registered_store(
    reference: ExternalArtifactReference,
    store: DataStore,
    *,
    authority: StoreAuthorityRegistry | None = None,
    lookup: _DataStoreLookup | _ExactDataStoreLookup | None = None,
    project_id: UUID | None = None,
    byte_start: int | None = None,
    byte_end: int | None = None,
) -> _StoreResolution:
    """Prepare and resolve one registered-store reference end to end.

    ``authority`` is the one registry snapshot the use-time boundary is allowed
    to capture.  Passing ``None`` installs :class:`ExplodingSnapshotProvider`
    instead, so the caller asserts that the path under test denied before any
    snapshot capture, DNS, network, credential, or subprocess work could begin.
    """

    events: list[str] = []
    resolver = _ResolverRegistry(events)
    lookup = lookup if lookup is not None else _DataStoreLookup(store)
    queries = ContextQueries(
        api=_ContextApi(
            project_id=project_id if project_id is not None else store.project_id,
            reference=reference,
        ),
        repository=_ContextRepository(lookup),  # type: ignore[arg-type]
        session=object(),
        release_read_scope=lambda: events.append("release"),
        resolver_registry=resolver,  # type: ignore[arg-type]
        store_authority_snapshot_provider=(
            RecordingSnapshotProvider(authority, events=events)
            if authority is not None
            else ExplodingSnapshotProvider()
        ),
    )
    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=byte_start,
        byte_end=byte_end,
    )
    result = queries.resolve_prepared_external_artifact(prepared)
    return _StoreResolution(
        prepared=prepared,
        result=result,
        events=events,
        resolver=resolver,
        lookup=lookup,
    )


def _assert_denied_before_any_remote_work(
    resolution: _StoreResolution,
    *,
    detail: str,
) -> None:
    """Assert one opaque denial that never reached a resolver or a target."""

    assert resolution.events == ["release"]
    assert resolution.resolver.prepared_targets == []
    assert resolution.resolver.parameters == []
    assert resolution.resolver.references == []
    assert resolution.resolver.local_targets == []
    assert resolution.resolver.http_targets == []
    assert resolution.resolver.rclone_targets == []
    assert resolution.resolver.git_targets == []
    assert type(resolution.prepared.target) is ResolvedArtifact
    assert resolution.prepared.target.status is ResolutionStatus.UNRESOLVED
    assert resolution.prepared.target.uri == "store://[redacted]"
    assert resolution.result["status"] == "unresolved"
    assert resolution.result["source_system"] == "store"
    assert resolution.result["uri"] == "store://[redacted]"
    assert resolution.result["observed_hash"] is None
    assert resolution.result["content_base64"] is None
    assert resolution.result["returned_bytes"] == 0
    assert resolution.result["detail"] == detail


def test_structured_and_uri_http_store_references_prepare_equal_targets():
    project_id = uuid4()
    store_name = "user@web store:one"
    logical_uri = "store://user%40web%20store%3Aone/caf%C3%A9/file%20name.bin"
    store, authority, _ = bound_data_store(
        name=store_name,
        kind=StoreKind.HTTP,
        root="https://files.example/base",
        project_id=project_id,
    )
    structured = ExternalArtifactReference.for_http_store(
        store_name=store_name,
        locator="café/file name.bin",
        content_hash=_sha256(b"ok"),
    )
    uri_only = ExternalArtifactReference(
        source_system="legacy-http",
        uri=logical_uri,
        content_hash=_sha256(b"ok"),
    )

    structured_run = _resolve_registered_store(structured, store, authority=authority)
    uri_run = _resolve_registered_store(uri_only, store, authority=authority)

    assert structured_run.events == ["release", "authority", "resolve-prepared"]
    assert uri_run.events == ["release", "authority", "resolve-prepared"]
    structured_target = structured_run.resolver.http_targets[0]
    uri_target = uri_run.resolver.http_targets[0]
    assert structured_target == uri_target
    assert structured_target.logical_reference.source_system == "store"
    assert structured_target.logical_reference.store_name == store_name
    assert structured_target.logical_reference.uri == logical_uri
    assert structured_target.locator.components == ("café", "file name.bin")
    assert structured_target.logical_reference.locator == "café/file name.bin"
    identity = structured_target.authority_binding_identity
    assert identity is not None
    assert identity.store_id == store.store_id
    assert identity.grant_id == store.authority_grant_id
    assert identity.fingerprint == store.authority_grant_fingerprint
    assert store.authority_grant_fingerprint not in repr(structured_target)
    assert structured_run.result["status"] == "verified"
    assert uri_run.result["status"] == "verified"


def test_uri_http_store_reference_preserves_unique_legacy_literal_percent_name():
    project_id = uuid4()
    store, authority, _ = bound_data_store(
        name="legacy%20remote",
        kind=StoreKind.HTTP,
        root="https://files.example/base",
        project_id=project_id,
    )
    reference = ExternalArtifactReference(
        source_system="legacy-http",
        uri="store://legacy%20remote/nested/artifact.bin",
        content_hash=_sha256(b"ok"),
    )

    resolution = _resolve_registered_store(
        reference,
        store,
        authority=authority,
        lookup=_ExactDataStoreLookup(store),
    )

    assert resolution.lookup.names == ["legacy remote", "legacy%20remote"]
    assert resolution.events == ["release", "authority", "resolve-prepared"]
    target = resolution.resolver.http_targets[0]
    assert target.logical_reference.store_name == "legacy%20remote"
    assert target.logical_reference.uri == (
        "store://legacy%2520remote/nested/artifact.bin"
    )
    assert target.locator.path == "nested/artifact.bin"


def test_ambiguous_encoded_and_legacy_http_store_names_fail_closed():
    project_id = uuid4()
    decoded_store, _, _ = bound_data_store(
        name="legacy remote",
        kind=StoreKind.HTTP,
        root="https://files.example/decoded",
        project_id=project_id,
        grant_id="decoded-name-grant",
    )
    literal_store, _, _ = bound_data_store(
        name="legacy%20remote",
        kind=StoreKind.HTTP,
        root="https://files.example/literal",
        project_id=project_id,
        grant_id="literal-name-grant",
    )
    reference = ExternalArtifactReference(
        source_system="legacy-http",
        uri="store://legacy%20remote/nested/artifact.bin",
        content_hash=_sha256(b"ok"),
    )

    resolution = _resolve_registered_store(
        reference,
        decoded_store,
        lookup=_ExactDataStoreLookup(decoded_store, literal_store),
        project_id=project_id,
    )

    assert resolution.lookup.names == ["legacy remote", "legacy%20remote"]
    _assert_denied_before_any_remote_work(
        resolution,
        detail="Store artifact reference is invalid.",
    )


@pytest.mark.parametrize(
    ("locator", "canonical_uri"),
    (
        (
            "nested/file name.bin",
            "store://web/nested/file%20name.bin",
        ),
        (
            "Müller/測定.bin",
            "store://web/M%C3%BCller/%E6%B8%AC%E5%AE%9A.bin",
        ),
        (
            "nested/a+b.bin",
            "store://web/nested/a%2Bb.bin",
        ),
        (
            "nested/result (final).bin",
            "store://web/nested/result%20%28final%29.bin",
        ),
    ),
)
def test_legacy_for_store_http_reference_is_canonicalized_during_preparation(
    locator: str,
    canonical_uri: str,
) -> None:
    project_id = uuid4()
    reference = ExternalArtifactReference.for_store(
        store_name="web",
        locator=locator,
        content_hash=_sha256(b"ok"),
        source_system="legacy-http",
    )
    assert reference.uri == f"store://web/{locator}"
    store, authority, _ = bound_data_store(
        name="web",
        kind=StoreKind.HTTP,
        root="https://files.example/base",
        project_id=project_id,
    )

    resolution = _resolve_registered_store(reference, store, authority=authority)

    assert resolution.events == ["release", "authority", "resolve-prepared"]
    target = resolution.resolver.http_targets[0]
    assert target.locator.path == locator
    assert target.logical_reference.source_system == "store"
    assert target.logical_reference.uri == canonical_uri


@pytest.mark.parametrize("uri_only", (False, True))
def test_http_store_legacy_compatibility_does_not_accept_mismatched_identity(
    uri_only: bool,
) -> None:
    project_id = uuid4()
    legacy = ExternalArtifactReference.for_store(
        store_name="web",
        locator="nested/file name.bin",
        content_hash=_sha256(b"ok"),
    )
    reference = (
        ExternalArtifactReference(
            source_system="store",
            uri=legacy.uri,
            content_hash=legacy.content_hash,
        )
        if uri_only
        else legacy.model_copy(update={"uri": "store://web/nested/different name.bin"})
    )
    store, _, _ = bound_data_store(
        name="web",
        kind=StoreKind.HTTP,
        root="https://files.example/base",
        project_id=project_id,
    )

    resolution = _resolve_registered_store(reference, store)

    assert resolution.lookup.names == ["web"]
    _assert_denied_before_any_remote_work(
        resolution,
        detail="Store artifact reference is invalid.",
    )


@pytest.mark.parametrize(
    "store_name",
    (
        "[not-ip]",
        "web：name",
        "web／name",
    ),
)
def test_non_round_trippable_http_store_names_fail_in_both_reference_forms(
    store_name: str,
) -> None:
    project_id = uuid4()
    logical_uri = f"store://{store_name}/nested/artifact.bin"
    references = (
        ExternalArtifactReference(
            source_system="store",
            uri=logical_uri,
            content_hash=_sha256(b"ok"),
            store_name=store_name,
            locator="nested/artifact.bin",
        ),
        ExternalArtifactReference(
            source_system="store",
            uri=logical_uri,
            content_hash=_sha256(b"ok"),
        ),
    )
    details: list[object] = []

    for reference in references:
        store, _, _ = bound_data_store(
            name="round-trippable",
            kind=StoreKind.HTTP,
            root="https://files.example/base",
            project_id=project_id,
        )
        # A name the definition validator rejects can no longer be registered,
        # so the only way such a row exists is as a legacy or corrupted record
        # that still carries authority columns.
        store.name = store_name

        resolution = _resolve_registered_store(reference, store)

        assert resolution.resolver.prepared_targets == []
        assert resolution.resolver.http_targets == []
        assert resolution.events == ["release"]
        assert resolution.result["uri"] == "store://[redacted]"
        assert resolution.result["status"] == "unresolved"
        details.append(resolution.result["detail"])

    assert details == [
        # The structured form selects the row and fails when its persisted
        # identity cannot be revalidated; the URI-only form never yields a
        # candidate name at all.
        "Store artifact could not be resolved.",
        "Store artifact reference is invalid.",
    ]


@pytest.mark.parametrize(
    ("root", "uri", "store_name", "locator", "expected_detail"),
    (
        (
            "https://user:secret@files.example/base",
            "store://web/artifact.bin",
            "web",
            "artifact.bin",
            "Store artifact could not be resolved.",
        ),
        (
            "https://files.example/base?token=secret",
            "store://web/artifact.bin",
            "web",
            "artifact.bin",
            "Store artifact could not be resolved.",
        ),
        (
            "https://files.example/base/",
            "store://web/%2e%2e/secret.bin",
            None,
            None,
            "Store artifact reference is invalid.",
        ),
        (
            "https://files.example/base/",
            "store://web/nested%5Csecret.bin",
            None,
            None,
            "Store artifact reference is invalid.",
        ),
    ),
)
def test_invalid_http_store_definition_or_locator_never_reaches_resolver_io(
    root: str,
    uri: str,
    store_name: str | None,
    locator: str | None,
    expected_detail: str,
):
    project_id = uuid4()
    reference = ExternalArtifactReference(
        source_system="store",
        uri=uri,
        content_hash=_sha256(b"secret"),
        store_name=store_name,
        locator=locator,
    )
    store, _, _ = bound_data_store(
        name="web",
        kind=StoreKind.HTTP,
        root="https://files.example/base",
        project_id=project_id,
    )
    store.root = root

    resolution = _resolve_registered_store(reference, store)

    _assert_denied_before_any_remote_work(resolution, detail=expected_detail)
    assert "secret" not in str(resolution.result)


def test_structured_and_uri_rclone_store_references_prepare_equal_targets():
    project_id = uuid4()
    store_name = "cloud store"
    logical_uri = "store://cloud%20store/caf%C3%A9/file%20name.bin"
    metadata = {"nested": {"value": "prepared"}}
    store, authority, _ = bound_data_store(
        name=store_name,
        kind=StoreKind.RCLONE,
        root="/Lab Data",
        credential_ref="lab remote",
        project_id=project_id,
    )
    structured = ExternalArtifactReference.for_store(
        store_name=store_name,
        locator="café/file name.bin",
        content_hash=_sha256(b"ok"),
        source_system="legacy-rclone",
        metadata=metadata,
    )
    uri_only = ExternalArtifactReference(
        source_system="legacy-rclone",
        uri=logical_uri,
        content_hash=_sha256(b"ok"),
        metadata=metadata,
    )

    structured_run = _resolve_registered_store(structured, store, authority=authority)
    uri_run = _resolve_registered_store(uri_only, store, authority=authority)

    assert structured.uri == "store://cloud store/café/file name.bin"
    assert structured_run.events == ["release", "authority", "resolve-prepared"]
    assert uri_run.events == ["release", "authority", "resolve-prepared"]
    structured_target = structured_run.resolver.rclone_targets[0]
    uri_target = uri_run.resolver.rclone_targets[0]
    assert structured_target == uri_target
    assert structured_target.logical_reference.source_system == "store"
    assert structured_target.logical_reference.store_name == store_name
    assert structured_target.logical_reference.uri == logical_uri
    assert structured_target.logical_reference.locator == "café/file name.bin"
    assert structured_target.remote.value == "lab remote"
    assert structured_target.registered_root.rooted is True
    assert structured_target.registered_root.components == ("Lab Data",)
    assert structured_target.locator.components == ("café", "file name.bin")
    with pytest.raises(FrozenInstanceError):
        structured_target.locator = uri_target.locator


def test_rclone_preparation_preserves_relative_rooted_and_remote_root_modes():
    project_id = uuid4()
    reference = ExternalArtifactReference.for_store(
        store_name="archive",
        locator="artifact.bin",
        content_hash=_sha256(b"ok"),
    )

    def prepare(root: str) -> RcloneStoreResolutionTarget:
        store, authority, _ = bound_data_store(
            name="archive",
            kind=StoreKind.ONEDRIVE,
            root=root,
            credential_ref="lab-onedrive",
            project_id=project_id,
        )
        resolution = _resolve_registered_store(reference, store, authority=authority)
        assert resolution.events == ["release", "authority", "resolve-prepared"]
        return resolution.resolver.rclone_targets[0]

    relative = prepare("experiments")
    rooted = prepare("/experiments")
    remote_root = prepare("/")

    assert relative != rooted
    assert relative.registered_root.rooted is False
    assert relative.registered_root.components == ("experiments",)
    assert relative.argv_target == "lab-onedrive:experiments/artifact.bin"
    assert rooted.registered_root.rooted is True
    assert rooted.registered_root.components == ("experiments",)
    assert rooted.argv_target == "lab-onedrive:/experiments/artifact.bin"
    assert remote_root.registered_root.rooted is True
    assert remote_root.registered_root.components == ()
    assert remote_root.argv_target == "lab-onedrive:/artifact.bin"


def test_present_empty_rclone_credential_fails_closed_without_scoped_dispatch():
    project_id = uuid4()
    reference = ExternalArtifactReference.for_store(
        store_name="archive",
        locator="nested/artifact.bin",
        content_hash=_sha256(b"secret"),
    )
    store, _, _ = bound_data_store(
        name="archive",
        kind=StoreKind.RCLONE,
        root="/experiments",
        credential_ref="lab-remote",
        project_id=project_id,
    )
    store.credential_ref = ""

    resolution = _resolve_registered_store(reference, store)

    _assert_denied_before_any_remote_work(
        resolution,
        detail="Store artifact could not be resolved.",
    )
    assert "archive" not in str(resolution.result)


def _corruptible_rclone_store(
    *,
    project_id: UUID,
    root: str,
    credential_ref: str,
) -> DataStore:
    """Mint a bound rclone row, then persist the exact root and remote wanted.

    Values the definition validator accepts round-trip unchanged; values it
    rejects can only reach the database as a legacy or corrupted row, so
    overwriting after binding is the one way to exercise them through the
    use-time boundary.
    """

    store, _, _ = bound_data_store(
        name="archive",
        kind=StoreKind.RCLONE,
        root="/experiments",
        credential_ref="lab-remote",
        project_id=project_id,
    )
    store.root = root
    store.credential_ref = credential_ref
    return store


@pytest.mark.parametrize(
    ("reference", "root", "credential_ref", "forbidden", "expected_detail"),
    (
        (
            ExternalArtifactReference(
                source_system="store",
                uri="store://archive/nested/artifact.bin?download=secret",
                content_hash=_sha256(b"secret"),
            ),
            "/experiments",
            "lab-remote",
            "download",
            "Store artifact reference is invalid.",
        ),
        (
            ExternalArtifactReference(
                source_system="store",
                uri="store://archive/nested/artifact.bin#secret-fragment",
                content_hash=_sha256(b"secret"),
            ),
            "/experiments",
            "lab-remote",
            "fragment",
            "Store artifact reference is invalid.",
        ),
        (
            ExternalArtifactReference(
                source_system="store",
                uri="store://archive/nested%2Fsecret.bin",
                content_hash=_sha256(b"secret"),
            ),
            "/experiments",
            "lab-remote",
            "secret.bin",
            "Store artifact reference is invalid.",
        ),
        (
            ExternalArtifactReference.for_store(
                store_name="archive",
                locator="../secret.bin",
                content_hash=_sha256(b"secret"),
            ),
            "/experiments",
            "lab-remote",
            "secret.bin",
            "Store artifact reference is invalid.",
        ),
        (
            ExternalArtifactReference.for_store(
                store_name="archive",
                locator="artifact.bin",
                content_hash=_sha256(b"secret"),
            ),
            "../outside",
            "lab-remote",
            "outside",
            "Store artifact could not be resolved.",
        ),
        (
            ExternalArtifactReference.for_store(
                store_name="archive",
                locator="artifact.bin",
                content_hash=_sha256(b"secret"),
            ),
            "/experiments",
            ":s3,env_auth=true",
            "env_auth",
            "Store artifact could not be resolved.",
        ),
        (
            ExternalArtifactReference.for_store(
                store_name="archive",
                locator="artifact.bin",
                content_hash=_sha256(b"secret"),
            ),
            "/experiments",
            "-config",
            "config",
            "Store artifact could not be resolved.",
        ),
    ),
)
def test_invalid_rclone_identity_locator_root_or_remote_fails_opaquely(
    reference: ExternalArtifactReference,
    root: str,
    credential_ref: str,
    forbidden: str,
    expected_detail: str,
) -> None:
    store = _corruptible_rclone_store(
        project_id=uuid4(),
        root=root,
        credential_ref=credential_ref,
    )

    resolution = _resolve_registered_store(reference, store)

    _assert_denied_before_any_remote_work(resolution, detail=expected_detail)
    assert forbidden not in str(resolution.result)


def test_structured_and_uri_git_store_references_prepare_equal_targets():
    project_id = uuid4()
    object_id = "d" * 64
    store_name = "user@analysis store"
    canonical_uri = (
        "store://user%40analysis%20store/"
        f"M%C3%BCller/@generated%20model.py@{object_id}"
    )
    metadata = {"nested": {"value": "prepared"}}
    store, authority, _ = bound_data_store(
        name=store_name,
        kind=StoreKind.GIT,
        root="HTTPS://GIT.EXAMPLE:443/lab/model.git",
        project_id=project_id,
    )
    assert store.root == "https://git.example/lab/model.git"
    structured = ExternalArtifactReference.for_store(
        store_name=store_name,
        locator=f"Müller/@generated model.py@{object_id}",
        content_hash=_sha256(b"ok"),
        source_system="legacy-git",
        metadata=metadata,
    )
    uri_only = ExternalArtifactReference(
        source_system="legacy-git",
        uri=canonical_uri,
        content_hash=_sha256(b"ok"),
        metadata=metadata,
    )

    structured_run = _resolve_registered_store(structured, store, authority=authority)
    uri_run = _resolve_registered_store(uri_only, store, authority=authority)

    assert structured_run.events == ["release", "authority", "resolve-prepared"]
    assert uri_run.events == ["release", "authority", "resolve-prepared"]
    structured_target = structured_run.resolver.git_targets[0]
    uri_target = uri_run.resolver.git_targets[0]
    assert structured_target == uri_target
    assert structured_target.logical_reference.source_system == "store"
    assert structured_target.logical_reference.uri == canonical_uri
    assert structured_target.logical_reference.store_name == store_name
    assert structured_target.logical_reference.locator == (
        f"Müller/@generated model.py@{object_id}"
    )
    assert structured_target.remote.subprocess_value == (
        "https://git.example/lab/model.git"
    )
    assert structured_target.pin.path.components == (
        "Müller",
        "@generated model.py",
    )
    assert structured_target.pin.object_id.object_format == "sha256"
    with pytest.raises(FrozenInstanceError):
        structured_target.pin = uri_target.pin


@pytest.mark.parametrize(
    ("reference", "root", "forbidden", "expected_detail"),
    (
        (
            ExternalArtifactReference(
                source_system="store",
                uri=f"store://analysis-repo/src/model.py@{'a' * 40}?secret=query",
                content_hash=_sha256(b"secret"),
            ),
            "https://git.example/lab/model.git",
            "query",
            "Store artifact reference is invalid.",
        ),
        (
            ExternalArtifactReference(
                source_system="store",
                uri=f"store://analysis-repo/src/model.py%40{'a' * 40}",
                content_hash=_sha256(b"secret"),
            ),
            "https://git.example/lab/model.git",
            "model.py",
            "Store artifact reference is invalid.",
        ),
        (
            ExternalArtifactReference.for_store(
                store_name="analysis-repo",
                locator="src/model.py@HEAD",
                content_hash=_sha256(b"secret"),
            ),
            "https://git.example/lab/model.git",
            "HEAD",
            "Store artifact reference is invalid.",
        ),
        (
            ExternalArtifactReference.for_store(
                store_name="analysis-repo",
                locator=f"src/model.py@{'A' * 40}",
                content_hash=_sha256(b"secret"),
            ),
            "https://git.example/lab/model.git",
            "AAAA",
            "Store artifact reference is invalid.",
        ),
        (
            ExternalArtifactReference.for_store(
                store_name="analysis-repo",
                locator=f"src/../secret.py@{'a' * 40}",
                content_hash=_sha256(b"secret"),
            ),
            "https://git.example/lab/model.git",
            "secret.py",
            "Store artifact reference is invalid.",
        ),
        (
            ExternalArtifactReference.for_store(
                store_name="analysis-repo",
                locator=f"src/model.py@{'a' * 40}",
                content_hash=_sha256(b"secret"),
            ),
            "../legacy-secret-repo",
            "legacy-secret",
            "Store artifact could not be resolved.",
        ),
    ),
)
def test_invalid_git_identity_pin_path_or_remote_fails_opaquely(
    reference: ExternalArtifactReference,
    root: str,
    forbidden: str,
    expected_detail: str,
) -> None:
    store, _, _ = bound_data_store(
        name="analysis-repo",
        kind=StoreKind.GIT,
        root="https://git.example/lab/model.git",
        project_id=uuid4(),
    )
    store.root = root

    resolution = _resolve_registered_store(reference, store)

    _assert_denied_before_any_remote_work(resolution, detail=expected_detail)
    assert forbidden not in str(resolution.result)


def test_preexisting_raw_at_git_pin_keeps_one_canonical_store_identity():
    project_id = uuid4()
    commit = "a" * 40
    locator = f"src/model.py@{commit}"
    reference = ExternalArtifactReference(
        source_system="store",
        uri=f"store://analysis-repo/{locator}",
        content_hash=_sha256(b"ok"),
        store_name="analysis-repo",
        locator=locator,
    )
    store, authority, _ = bound_data_store(
        name="analysis-repo",
        kind=StoreKind.GIT,
        root="https://git.example/lab/model.git",
        project_id=project_id,
    )

    resolution = _resolve_registered_store(reference, store, authority=authority)

    assert resolution.events == ["release", "authority", "resolve-prepared"]
    target = resolution.resolver.git_targets[0]
    assert target.logical_reference.uri == (
        f"store://analysis-repo/src/model.py@{commit}"
    )
    assert target.logical_reference.locator == locator
    assert target.remote.subprocess_value == "https://git.example/lab/model.git"
    assert target.pin.path.path == "src/model.py"
    assert target.pin.object_id.value == commit
    assert resolution.lookup.names == ["analysis-repo"]


def test_git_store_materialization_rejects_nonportable_ads_punctuation():
    project_id = uuid4()
    commit = "b" * 40
    reference = ExternalArtifactReference.for_store(
        store_name="analysis-repo",
        locator=f"src/run:1.py@{commit}",
        content_hash=_sha256(b"model"),
        source_system="legacy-git",
    )
    store, _, _ = bound_data_store(
        name="analysis-repo",
        kind=StoreKind.GIT,
        root="https://git.example/lab/model.git",
        project_id=project_id,
    )

    resolution = _resolve_registered_store(reference, store)

    _assert_denied_before_any_remote_work(
        resolution,
        detail="Store artifact reference is invalid.",
    )
    assert resolution.lookup.names == ["analysis-repo"]


def test_uri_only_git_store_reference_canonicalizes_custom_store_identity():
    project_id = uuid4()
    commit = "c" * 40
    store_name = "user@analysis-repo"
    reference = ExternalArtifactReference(
        source_system="legacy-git",
        uri=f"store://{store_name}/src/run.py@{commit}",
        content_hash=_sha256(b"ok"),
    )
    store, authority, _ = bound_data_store(
        name=store_name,
        kind=StoreKind.GIT,
        root="https://git.example/lab/model.git",
        project_id=project_id,
    )

    resolution = _resolve_registered_store(reference, store, authority=authority)

    assert resolution.events == ["release", "authority", "resolve-prepared"]
    target = resolution.resolver.git_targets[0]
    assert target.logical_reference.source_system == "store"
    assert target.logical_reference.uri == (
        f"store://user%40analysis-repo/src/run.py@{commit}"
    )
    assert target.logical_reference.store_name == store_name
    assert target.logical_reference.locator == f"src/run.py@{commit}"
    assert target.remote.subprocess_value == "https://git.example/lab/model.git"
    assert resolution.lookup.names == [store_name]


def test_canonicalized_identity_still_needs_the_operation_capability():
    project_id = uuid4()
    reference = ExternalArtifactReference.for_store(
        store_name="web",
        locator="nested/artifact.bin",
        content_hash=_sha256(b"ok"),
    )
    store, authority, _ = bound_data_store(
        name="web",
        kind=StoreKind.HTTP,
        root="https://files.example/base",
        capabilities=(StoreCapability.BYTES_BY_PATH,),
        project_id=project_id,
    )

    whole = _resolve_registered_store(reference, store, authority=authority)
    ranged = _resolve_registered_store(
        reference,
        store,
        authority=authority,
        byte_start=0,
        byte_end=1,
    )

    assert whole.events == ["release", "authority", "resolve-prepared"]
    assert whole.resolver.http_targets[0].logical_reference.uri == (
        "store://web/nested/artifact.bin"
    )
    _assert_denied_before_any_remote_work(
        ranged,
        detail="Store artifact could not be resolved.",
    )
