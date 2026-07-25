"""SQLAlchemy persistence for acquisition collections.

Snapshot summary rows and manifest JSON are deliberately accessed through
separate methods so bounded read models never materialize member lists.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.collection_db_models import (
    AcquisitionCollectionCaptureModel,
    AcquisitionCollectionManifestModel,
    AcquisitionCollectionModel,
    AcquisitionCollectionSnapshotModel,
    DatasetCollectionSnapshotLinkModel,
)
from lab_tracker.collection_models import (
    AcquisitionCollection,
    AcquisitionCollectionCapture,
    AcquisitionCollectionSnapshot,
    AcquisitionCollectionSummary,
    snapshot_with_capture_observation,
)

from .common import apply_pagination, count_from_statement


def _collection_from_row(
    row: AcquisitionCollectionModel,
) -> AcquisitionCollection:
    return AcquisitionCollection(
        collection_id=row.collection_id,
        session_id=row.session_id,
        collection_key=row.collection_key,
        current_snapshot_id=row.current_snapshot_id,
        current_capture_id=row.current_capture_id,
        current_observed_at=row.current_observed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _snapshot_from_row(
    row: AcquisitionCollectionSnapshotModel,
) -> AcquisitionCollectionSnapshot:
    return AcquisitionCollectionSnapshot(
        snapshot_id=row.snapshot_id,
        collection_id=row.collection_id,
        manifest_hash=row.manifest_hash,
        member_count=row.member_count,
        total_size_bytes=row.total_size_bytes,
        complete=row.complete,
        source_provider=row.source_provider,
        source_uri=row.source_uri,
        observed_at=row.observed_at,
        sealed_at=row.sealed_at,
        client_capture_id=row.client_capture_id,
        capture_actor_user_id=row.capture_actor_user_id,
        capture_principal_type=row.capture_principal_type,
        capture_principal_instance_id=row.capture_principal_instance_id,
        capture_principal_label=row.capture_principal_label,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _capture_from_row(
    row: AcquisitionCollectionCaptureModel,
) -> AcquisitionCollectionCapture:
    return AcquisitionCollectionCapture(
        capture_id=row.capture_id,
        collection_id=row.collection_id,
        client_capture_id=row.client_capture_id,
        snapshot_id=row.snapshot_id,
        request_hash=row.request_hash,
        observed_at=row.observed_at,
        complete=row.complete,
        source_provider=row.source_provider,
        source_uri=row.source_uri,
        capture_actor_user_id=row.capture_actor_user_id,
        capture_principal_type=row.capture_principal_type,
        capture_principal_instance_id=row.capture_principal_instance_id,
        capture_principal_label=row.capture_principal_label,
        created_at=row.created_at,
    )


class SQLAlchemyAcquisitionCollectionRepository:
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def get(self, collection_id: UUID) -> AcquisitionCollection | None:
        self._session.flush()
        row = self._session.get(
            AcquisitionCollectionModel,
            str(collection_id),
        )
        return None if row is None else _collection_from_row(row)

    def get_by_session_key(
        self,
        *,
        session_id: UUID,
        collection_key: str,
        for_update: bool = False,
    ) -> AcquisitionCollection | None:
        self._session.flush()
        stmt = select(AcquisitionCollectionModel).where(
            AcquisitionCollectionModel.session_id == str(session_id),
            AcquisitionCollectionModel.collection_key == collection_key,
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = self._session.scalar(stmt)
        return None if row is None else _collection_from_row(row)

    def get_or_create(
        self,
        collection: AcquisitionCollection,
    ) -> AcquisitionCollection:
        """Atomically resolve the collection identity, then lock its row."""

        values = {
            "collection_id": collection.collection_id,
            "session_id": collection.session_id,
            "collection_key": collection.collection_key,
            "current_snapshot_id": collection.current_snapshot_id,
            "current_capture_id": collection.current_capture_id,
            "current_observed_at": collection.current_observed_at,
            "created_at": collection.created_at,
            "updated_at": collection.updated_at,
        }
        dialect_name = self._session.get_bind().dialect.name
        if dialect_name == "sqlite":
            statement = sqlite_insert(AcquisitionCollectionModel)
        elif dialect_name == "postgresql":
            statement = postgresql_insert(AcquisitionCollectionModel)
        else:
            existing = self.get_by_session_key(
                session_id=collection.session_id,
                collection_key=collection.collection_key,
                for_update=True,
            )
            if existing is not None:
                return existing
            self.save(collection)
            return collection
        self._session.execute(
            statement.values(**values).on_conflict_do_nothing(
                index_elements=("session_id", "collection_key"),
            )
        )
        self._session.flush()
        persisted = self.get_by_session_key(
            session_id=collection.session_id,
            collection_key=collection.collection_key,
            for_update=True,
        )
        if persisted is None:
            raise RuntimeError(
                "Collection upsert did not return a persisted row."
            )
        return persisted

    def query(
        self,
        *,
        session_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[AcquisitionCollectionSummary], int]:
        self._session.flush()
        stmt = select(AcquisitionCollectionModel)
        count_stmt = select(AcquisitionCollectionModel.collection_id)
        if session_id is not None:
            stmt = stmt.where(
                AcquisitionCollectionModel.session_id == str(session_id)
            )
            count_stmt = count_stmt.where(
                AcquisitionCollectionModel.session_id == str(session_id)
            )
        stmt = stmt.order_by(
            AcquisitionCollectionModel.created_at,
            AcquisitionCollectionModel.collection_id,
        )
        total = count_from_statement(self._session, count_stmt)
        rows = list(
            self._session.scalars(
                apply_pagination(stmt, limit=limit, offset=offset)
            )
        )
        snapshot_ids = [
            row.current_snapshot_id
            for row in rows
            if row.current_snapshot_id is not None
        ]
        snapshots: dict[UUID, AcquisitionCollectionSnapshot] = {}
        if snapshot_ids:
            snapshot_rows = self._session.scalars(
                select(AcquisitionCollectionSnapshotModel).where(
                    AcquisitionCollectionSnapshotModel.snapshot_id.in_(
                        snapshot_ids
                    )
                )
            )
            snapshots = {
                row.snapshot_id: _snapshot_from_row(row)
                for row in snapshot_rows
            }
        capture_ids = [
            row.current_capture_id
            for row in rows
            if row.current_capture_id is not None
        ]
        captures: dict[UUID, AcquisitionCollectionCapture] = {}
        if capture_ids:
            capture_rows = self._session.scalars(
                select(AcquisitionCollectionCaptureModel).where(
                    AcquisitionCollectionCaptureModel.capture_id.in_(
                        capture_ids
                    )
                )
            )
            captures = {
                row.capture_id: _capture_from_row(row)
                for row in capture_rows
            }
        return (
            [
                AcquisitionCollectionSummary(
                    **_collection_from_row(row).model_dump(),
                    current_snapshot=(
                        snapshot_with_capture_observation(
                            snapshots[row.current_snapshot_id],
                            captures.get(row.current_capture_id),
                        )
                        if row.current_snapshot_id in snapshots
                        else None
                    ),
                )
                for row in rows
            ],
            total,
        )

    def save(self, collection: AcquisitionCollection) -> None:
        row = self._session.get(
            AcquisitionCollectionModel,
            str(collection.collection_id),
        )
        if row is None:
            self._session.add(
                AcquisitionCollectionModel(
                    collection_id=collection.collection_id,
                    session_id=collection.session_id,
                    collection_key=collection.collection_key,
                    current_snapshot_id=collection.current_snapshot_id,
                    current_capture_id=collection.current_capture_id,
                    current_observed_at=collection.current_observed_at,
                    created_at=collection.created_at,
                    updated_at=collection.updated_at,
                )
            )
            self._session.flush()
            return
        if (
            row.session_id != collection.session_id
            or row.collection_key != collection.collection_key
        ):
            raise ValueError("Collection identity is immutable.")
        row.current_snapshot_id = collection.current_snapshot_id
        row.current_capture_id = collection.current_capture_id
        row.current_observed_at = collection.current_observed_at
        row.updated_at = collection.updated_at
        self._session.flush()

    def get_snapshot(
        self,
        snapshot_id: UUID | None,
    ) -> AcquisitionCollectionSnapshot | None:
        if snapshot_id is None:
            return None
        self._session.flush()
        row = self._session.get(
            AcquisitionCollectionSnapshotModel,
            str(snapshot_id),
        )
        return None if row is None else _snapshot_from_row(row)

    def get_snapshot_by_hash(
        self,
        *,
        collection_id: UUID,
        manifest_hash: str,
    ) -> AcquisitionCollectionSnapshot | None:
        self._session.flush()
        row = self._session.scalar(
            select(AcquisitionCollectionSnapshotModel).where(
                AcquisitionCollectionSnapshotModel.collection_id
                == str(collection_id),
                AcquisitionCollectionSnapshotModel.manifest_hash
                == manifest_hash,
            )
        )
        return None if row is None else _snapshot_from_row(row)

    def query_snapshots(
        self,
        *,
        collection_id: UUID,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[AcquisitionCollectionSnapshot], int]:
        self._session.flush()
        stmt = select(AcquisitionCollectionSnapshotModel).where(
            AcquisitionCollectionSnapshotModel.collection_id
            == str(collection_id)
        )
        count_stmt = select(
            AcquisitionCollectionSnapshotModel.snapshot_id
        ).where(
            AcquisitionCollectionSnapshotModel.collection_id
            == str(collection_id)
        )
        stmt = stmt.order_by(
            AcquisitionCollectionSnapshotModel.observed_at.desc(),
            AcquisitionCollectionSnapshotModel.snapshot_id.desc(),
        )
        total = count_from_statement(self._session, count_stmt)
        rows = list(
            self._session.scalars(
                apply_pagination(stmt, limit=limit, offset=offset)
            )
        )
        return [_snapshot_from_row(row) for row in rows], total

    def save_snapshot(
        self,
        snapshot: AcquisitionCollectionSnapshot,
    ) -> None:
        row = self._session.get(
            AcquisitionCollectionSnapshotModel,
            str(snapshot.snapshot_id),
        )
        if row is None:
            self._session.add(
                AcquisitionCollectionSnapshotModel(
                    snapshot_id=snapshot.snapshot_id,
                    collection_id=snapshot.collection_id,
                    manifest_hash=snapshot.manifest_hash,
                    member_count=snapshot.member_count,
                    total_size_bytes=snapshot.total_size_bytes,
                    complete=snapshot.complete,
                    source_provider=snapshot.source_provider,
                    source_uri=snapshot.source_uri,
                    observed_at=snapshot.observed_at,
                    sealed_at=snapshot.sealed_at,
                    client_capture_id=snapshot.client_capture_id,
                    capture_actor_user_id=snapshot.capture_actor_user_id,
                    capture_principal_type=snapshot.capture_principal_type,
                    capture_principal_instance_id=(
                        snapshot.capture_principal_instance_id
                    ),
                    capture_principal_label=snapshot.capture_principal_label,
                    created_at=snapshot.created_at,
                    updated_at=snapshot.updated_at,
                )
            )
            self._session.flush()
            return
        immutable = (
            row.collection_id,
            row.manifest_hash,
            row.member_count,
            row.total_size_bytes,
            row.source_provider,
            row.source_uri,
            row.observed_at,
            row.client_capture_id,
        )
        expected = (
            snapshot.collection_id,
            snapshot.manifest_hash,
            snapshot.member_count,
            snapshot.total_size_bytes,
            snapshot.source_provider,
            snapshot.source_uri,
            snapshot.observed_at,
            snapshot.client_capture_id,
        )
        if immutable != expected:
            raise ValueError("Collection snapshot content is immutable.")
        if row.complete and not snapshot.complete:
            raise ValueError("A sealed collection snapshot cannot be unsealed.")
        row.complete = snapshot.complete
        row.sealed_at = snapshot.sealed_at
        row.updated_at = snapshot.updated_at
        self._session.flush()

    def get_manifest(self, snapshot_id: UUID) -> dict[str, object] | None:
        self._session.flush()
        row = self._session.get(
            AcquisitionCollectionManifestModel,
            str(snapshot_id),
        )
        return None if row is None else dict(row.manifest_json)

    def save_manifest(
        self,
        *,
        snapshot_id: UUID,
        schema_version: int,
        manifest_json: dict[str, object],
        canonical_size_bytes: int,
    ) -> None:
        existing = self._session.get(
            AcquisitionCollectionManifestModel,
            str(snapshot_id),
        )
        if existing is not None:
            if (
                existing.schema_version != schema_version
                or existing.manifest_json != manifest_json
                or existing.canonical_size_bytes != canonical_size_bytes
            ):
                raise ValueError("Collection manifest content is immutable.")
            return
        self._session.add(
            AcquisitionCollectionManifestModel(
                snapshot_id=snapshot_id,
                schema_version=schema_version,
                manifest_json=manifest_json,
                canonical_size_bytes=canonical_size_bytes,
            )
        )
        self._session.flush()

    def get_capture(
        self,
        *,
        collection_id: UUID,
        client_capture_id: str,
    ) -> AcquisitionCollectionCapture | None:
        self._session.flush()
        row = self._session.scalar(
            select(AcquisitionCollectionCaptureModel).where(
                AcquisitionCollectionCaptureModel.collection_id
                == str(collection_id),
                AcquisitionCollectionCaptureModel.client_capture_id
                == client_capture_id,
            )
        )
        return None if row is None else _capture_from_row(row)

    def get_capture_by_id(
        self,
        capture_id: UUID,
    ) -> AcquisitionCollectionCapture | None:
        self._session.flush()
        row = self._session.get(
            AcquisitionCollectionCaptureModel,
            str(capture_id),
        )
        return None if row is None else _capture_from_row(row)

    def get_latest_capture_for_snapshot(
        self,
        snapshot_id: UUID,
    ) -> AcquisitionCollectionCapture | None:
        self._session.flush()
        row = self._session.scalar(
            select(AcquisitionCollectionCaptureModel)
            .where(
                AcquisitionCollectionCaptureModel.snapshot_id
                == str(snapshot_id)
            )
            .order_by(
                AcquisitionCollectionCaptureModel.observed_at.desc(),
                AcquisitionCollectionCaptureModel.created_at,
                AcquisitionCollectionCaptureModel.capture_id,
            )
            .limit(1)
        )
        return None if row is None else _capture_from_row(row)

    def save_capture(self, capture: AcquisitionCollectionCapture) -> None:
        existing = self.get_capture(
            collection_id=capture.collection_id,
            client_capture_id=capture.client_capture_id,
        )
        if existing is not None:
            if existing != capture:
                raise ValueError("Collection capture identity is immutable.")
            return
        self._session.add(
            AcquisitionCollectionCaptureModel(
                capture_id=capture.capture_id,
                collection_id=capture.collection_id,
                client_capture_id=capture.client_capture_id,
                snapshot_id=capture.snapshot_id,
                request_hash=capture.request_hash,
                observed_at=capture.observed_at,
                complete=capture.complete,
                source_provider=capture.source_provider,
                source_uri=capture.source_uri,
                capture_actor_user_id=capture.capture_actor_user_id,
                capture_principal_type=capture.capture_principal_type,
                capture_principal_instance_id=(
                    capture.capture_principal_instance_id
                ),
                capture_principal_label=capture.capture_principal_label,
                created_at=capture.created_at,
            )
        )
        self._session.flush()

    def dataset_ids_referencing_session(
        self,
        session_id: UUID,
    ) -> list[UUID]:
        """Return Datasets whose compact manifests name this Session's snapshots."""

        self._session.flush()
        rows = self._session.scalars(
            select(DatasetCollectionSnapshotLinkModel.dataset_id)
            .join(
                AcquisitionCollectionSnapshotModel,
                AcquisitionCollectionSnapshotModel.snapshot_id
                == DatasetCollectionSnapshotLinkModel.snapshot_id,
            )
            .join(
                AcquisitionCollectionModel,
                AcquisitionCollectionModel.collection_id
                == AcquisitionCollectionSnapshotModel.collection_id,
            )
            .where(AcquisitionCollectionModel.session_id == str(session_id))
            .distinct()
            .order_by(DatasetCollectionSnapshotLinkModel.dataset_id)
        )
        return list(rows)
