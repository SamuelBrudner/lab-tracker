"""Acquisition collection capture and bounded manifest reads."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.collection_manifest import (
    canonicalize_collection_key,
    canonicalize_collection_manifest,
)
from lab_tracker.collection_models import (
    AcquisitionCollection,
    AcquisitionCollectionCapture,
    AcquisitionCollectionCaptureResult,
    AcquisitionCollectionManifest,
    AcquisitionCollectionMember,
    AcquisitionCollectionSnapshot,
    AcquisitionCollectionSummary,
    snapshot_with_capture_observation,
)
from lab_tracker.errors import ConflictError, NotFoundError, ValidationError
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.session_service import SessionService
from lab_tracker.services.shared import actor_user_fk


class _CollectionPointerStore(Protocol):
    def get_snapshot(
        self,
        snapshot_id: UUID | None,
    ) -> AcquisitionCollectionSnapshot | None: ...

    def save(self, collection: AcquisitionCollection) -> None: ...


class AcquisitionCollectionService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        sessions: SessionService,
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.sessions = sessions
        self.authorization = authorization

    def capture_snapshot(
        self,
        *,
        session_id: UUID,
        collection_key: str,
        client_capture_id: str,
        observed_at: datetime,
        complete: bool,
        schema_version: int,
        members: Iterable[object],
        source_provider: str | None = None,
        source_uri: str | None = None,
        actor: AuthContext | None = None,
    ) -> AcquisitionCollectionCaptureResult:
        session = self.sessions.get_session(session_id)
        self.authorization.require_contributor(session.project_id, actor=actor)
        resolved_key = canonicalize_collection_key(collection_key)
        resolved_capture_id = _normalize_required_string(
            client_capture_id,
            field_name="client_capture_id",
            max_length=200,
        )
        resolved_observed_at = _normalize_observed_at(observed_at)
        resolved_source_provider = _normalize_optional_string(
            source_provider,
            field_name="source_provider",
            max_length=80,
        )
        resolved_source_uri = _normalize_optional_string(
            source_uri,
            field_name="source_uri",
            max_length=2_000,
        )
        canonical = canonicalize_collection_manifest(
            schema_version=schema_version,
            members=members,
        )
        request_hash = _capture_request_hash(
            manifest_hash=canonical.manifest_hash,
            observed_at=resolved_observed_at,
            complete=complete,
            source_provider=resolved_source_provider,
            source_uri=resolved_source_uri,
        )
        now = _utc_now()
        with self.application_transaction(), self.unit_of_work() as repository:
            # The Session acquisition lock is always the first state lock for
            # this command. Re-read after PostgreSQL may have waited/expired
            # ORM state, then keep pointer selection and writes in this same
            # transaction.
            repository.lock_session_acquisition_state(session_id)
            session = self.sessions.get_session(session_id)
            self.authorization.require_contributor(session.project_id, actor=actor)
            store = repository.acquisition_collections
            collection = store.get_or_create(
                AcquisitionCollection(
                    collection_id=uuid4(),
                    session_id=session_id,
                    collection_key=resolved_key,
                    created_at=now,
                    updated_at=now,
                )
            )

            prior_capture = store.get_capture(
                collection_id=collection.collection_id,
                client_capture_id=resolved_capture_id,
            )
            if prior_capture is not None:
                if prior_capture.request_hash != request_hash:
                    raise ConflictError(
                        "client_capture_id was already used with a different "
                        "collection snapshot request."
                    )
                snapshot = store.get_snapshot(prior_capture.snapshot_id)
                if snapshot is None:
                    raise ConflictError(
                        "The idempotent collection capture references a missing snapshot."
                    )
                return AcquisitionCollectionCaptureResult(
                    collection=collection,
                    snapshot=snapshot_with_capture_observation(
                        snapshot,
                        prior_capture,
                    ),
                    snapshot_reused=True,
                    current_pointer_changed=False,
                )

            snapshot = store.get_snapshot_by_hash(
                collection_id=collection.collection_id,
                manifest_hash=canonical.manifest_hash,
            )
            snapshot_reused = snapshot is not None
            capture_actor_user_id = actor_user_fk(actor, repository)
            if snapshot is None:
                snapshot = AcquisitionCollectionSnapshot(
                    snapshot_id=uuid4(),
                    collection_id=collection.collection_id,
                    manifest_hash=canonical.manifest_hash,
                    member_count=canonical.member_count,
                    total_size_bytes=canonical.total_size_bytes,
                    complete=complete,
                    source_provider=resolved_source_provider,
                    source_uri=resolved_source_uri,
                    observed_at=resolved_observed_at,
                    sealed_at=now if complete else None,
                    client_capture_id=resolved_capture_id,
                    capture_actor_user_id=capture_actor_user_id,
                    capture_principal_type=(
                        actor.principal_type.value if actor is not None else None
                    ),
                    capture_principal_instance_id=(
                        actor.principal_instance_id if actor is not None else None
                    ),
                    capture_principal_label=(
                        actor.principal_label if actor is not None else None
                    ),
                    created_at=now,
                    updated_at=now,
                )
                store.save_snapshot(snapshot)
                store.save_manifest(
                    snapshot_id=snapshot.snapshot_id,
                    schema_version=canonical.schema_version,
                    manifest_json=canonical.as_dict(),
                    canonical_size_bytes=len(canonical.canonical_bytes),
                )
            elif complete and not snapshot.complete:
                snapshot.complete = True
                snapshot.sealed_at = now
                snapshot.updated_at = now
                store.save_snapshot(snapshot)

            capture = AcquisitionCollectionCapture(
                capture_id=uuid4(),
                collection_id=collection.collection_id,
                client_capture_id=resolved_capture_id,
                snapshot_id=snapshot.snapshot_id,
                request_hash=request_hash,
                observed_at=resolved_observed_at,
                complete=complete,
                source_provider=resolved_source_provider,
                source_uri=resolved_source_uri,
                capture_actor_user_id=capture_actor_user_id,
                capture_principal_type=(
                    actor.principal_type.value if actor is not None else None
                ),
                capture_principal_instance_id=(
                    actor.principal_instance_id if actor is not None else None
                ),
                capture_principal_label=(
                    actor.principal_label if actor is not None else None
                ),
                created_at=now,
            )
            store.save_capture(capture)
            current_changed = _advance_current_pointer(
                collection=collection,
                snapshot=snapshot,
                capture=capture,
                store=store,
                now=now,
            )
        return AcquisitionCollectionCaptureResult(
            collection=collection,
            snapshot=snapshot_with_capture_observation(snapshot, capture),
            snapshot_reused=snapshot_reused,
            current_pointer_changed=current_changed,
        )

    def get_collection(
        self,
        collection_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> AcquisitionCollection:
        collection = self.repository.acquisition_collections.get(collection_id)
        if collection is None:
            raise NotFoundError("Acquisition collection does not exist.")
        self._require_collection_read(collection, actor=actor)
        return collection

    def list_collections(
        self,
        *,
        session_id: UUID,
        limit: int,
        offset: int,
        actor: AuthContext | None = None,
    ) -> tuple[list[AcquisitionCollectionSummary], int]:
        session = self.sessions.get_session(session_id)
        self.authorization.require_read(session.project_id, actor=actor)
        return self.repository.acquisition_collections.query(
            session_id=session_id,
            limit=limit,
            offset=offset,
        )

    def get_snapshot(
        self,
        snapshot_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> AcquisitionCollectionSnapshot:
        snapshot = self.repository.acquisition_collections.get_snapshot(snapshot_id)
        if snapshot is None:
            raise NotFoundError("Acquisition collection snapshot does not exist.")
        collection = self.repository.acquisition_collections.get(
            snapshot.collection_id
        )
        if collection is None:
            raise NotFoundError("Acquisition collection does not exist.")
        self._require_collection_read(collection, actor=actor)
        return self._snapshot_with_current_observation(
            snapshot,
            collection=collection,
        )

    def list_snapshots(
        self,
        *,
        collection_id: UUID,
        limit: int,
        offset: int,
        actor: AuthContext | None = None,
    ) -> tuple[list[AcquisitionCollectionSnapshot], int]:
        collection = self.get_collection(collection_id, actor=actor)
        snapshots, total = (
            self.repository.acquisition_collections.query_snapshots(
                collection_id=collection.collection_id,
                limit=limit,
                offset=offset,
            )
        )
        return (
            [
                self._snapshot_with_current_observation(
                    snapshot,
                    collection=collection,
                )
                for snapshot in snapshots
            ],
            total,
        )

    def get_manifest(
        self,
        snapshot_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> AcquisitionCollectionManifest:
        self.get_snapshot(snapshot_id, actor=actor)
        payload = self.repository.acquisition_collections.get_manifest(snapshot_id)
        if payload is None:
            raise NotFoundError("Acquisition collection manifest does not exist.")
        return AcquisitionCollectionManifest.model_validate(payload)

    def list_members(
        self,
        *,
        snapshot_id: UUID,
        limit: int,
        offset: int,
        query: str | None = None,
        actor: AuthContext | None = None,
    ) -> tuple[list[AcquisitionCollectionMember], int]:
        manifest = self.get_manifest(snapshot_id, actor=actor)
        members = manifest.members
        needle = (query or "").strip().casefold()
        if needle:
            members = [
                member
                for member in members
                if needle in member.path.casefold()
            ]
        total = len(members)
        return members[offset : offset + limit], total

    def _require_collection_read(
        self,
        collection: AcquisitionCollection,
        *,
        actor: AuthContext | None,
    ) -> None:
        session = self.sessions.get_session(collection.session_id)
        self.authorization.require_read(session.project_id, actor=actor)

    def _snapshot_with_current_observation(
        self,
        snapshot: AcquisitionCollectionSnapshot,
        *,
        collection: AcquisitionCollection,
    ) -> AcquisitionCollectionSnapshot:
        store = self.repository.acquisition_collections
        capture = None
        if (
            collection.current_snapshot_id == snapshot.snapshot_id
            and collection.current_capture_id is not None
        ):
            capture = store.get_capture_by_id(collection.current_capture_id)
        if capture is None:
            capture = store.get_latest_capture_for_snapshot(snapshot.snapshot_id)
        return snapshot_with_capture_observation(snapshot, capture)


def _advance_current_pointer(
    *,
    collection: AcquisitionCollection,
    snapshot: AcquisitionCollectionSnapshot,
    capture: AcquisitionCollectionCapture,
    store: _CollectionPointerStore,
    now: datetime,
) -> bool:
    observed_at = capture.observed_at
    current_observed_at = collection.current_observed_at
    if current_observed_at is not None and observed_at < current_observed_at:
        return False
    if current_observed_at is not None and observed_at == current_observed_at:
        if collection.current_snapshot_id == snapshot.snapshot_id:
            return False
        current_snapshot = store.get_snapshot(collection.current_snapshot_id)
        if (
            current_snapshot is not None
            and current_snapshot.manifest_hash == snapshot.manifest_hash
        ):
            return False
        raise ConflictError(
            "A different collection snapshot already has the same observed_at."
        )
    collection.current_snapshot_id = snapshot.snapshot_id
    collection.current_capture_id = capture.capture_id
    collection.current_observed_at = observed_at
    collection.updated_at = now
    store.save(collection)
    return True


def _capture_request_hash(
    *,
    manifest_hash: str,
    observed_at: datetime,
    complete: bool,
    source_provider: str | None,
    source_uri: str | None,
) -> str:
    payload = {
        "complete": complete,
        "manifest_hash": manifest_hash,
        "observed_at": observed_at.isoformat(),
        "source_provider": source_provider,
        "source_uri": source_uri,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_required_string(
    value: str,
    *,
    field_name: str,
    max_length: int,
) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string.")
    cleaned = value.strip()
    if not cleaned:
        raise ValidationError(f"{field_name} must not be empty.")
    if len(cleaned) > max_length:
        raise ValidationError(
            f"{field_name} must be {max_length} characters or fewer."
        )
    return cleaned


def _normalize_optional_string(
    value: str | None,
    *,
    field_name: str,
    max_length: int,
) -> str | None:
    if value is None:
        return None
    return _normalize_required_string(
        value,
        field_name=field_name,
        max_length=max_length,
    )


def _normalize_observed_at(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("observed_at must include a timezone.")
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
