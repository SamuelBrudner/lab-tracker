"""SQLAlchemy persistence for atomic evidence-bundle idempotency records."""

from __future__ import annotations

import sqlite3
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import EvidenceBundleModel
from lab_tracker.models import EvidenceBundleRecord
from lab_tracker.repository import (
    EvidenceBundleKeyRaceError,
    EvidenceBundleRepository,
)
from lab_tracker.sqlalchemy_mappers import (
    evidence_bundle_record_from_model,
    evidence_bundle_record_to_model,
)

EVIDENCE_BUNDLE_KEY_CONSTRAINT = "uq_evidence_bundles_project_creator_key"
_SQLITE_SCOPED_KEY_COLUMNS = (
    "evidence_bundles.project_id",
    "evidence_bundles.created_by",
    "evidence_bundles.idempotency_key",
)


def _is_evidence_bundle_key_race(exc: IntegrityError) -> bool:
    """Identify only the evidence-bundle scoped-key unique violation."""

    diagnostic = getattr(exc.orig, "diag", None)
    if getattr(diagnostic, "constraint_name", None) is not None:
        return diagnostic.constraint_name == EVIDENCE_BUNDLE_KEY_CONSTRAINT

    if not isinstance(exc.orig, sqlite3.IntegrityError):
        return False
    prefix = "unique constraint failed:"
    message = str(exc.orig).strip().lower()
    if not message.startswith(prefix):
        return False
    columns = tuple(
        column.strip()
        for column in message.removeprefix(prefix).strip().split(",")
    )
    return columns == _SQLITE_SCOPED_KEY_COLUMNS


class SQLAlchemyEvidenceBundleRepository(EvidenceBundleRepository):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def get(self, entity_id: UUID) -> EvidenceBundleRecord | None:
        self._session.flush()
        row = self._session.get(EvidenceBundleModel, str(entity_id))
        return evidence_bundle_record_from_model(row) if row is not None else None

    def list(self) -> list[EvidenceBundleRecord]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(EvidenceBundleModel).order_by(
                    EvidenceBundleModel.created_at,
                    EvidenceBundleModel.bundle_id,
                )
            )
        )
        return [evidence_bundle_record_from_model(row) for row in rows]

    def insert(self, entity: EvidenceBundleRecord) -> None:
        row = self._session.get(EvidenceBundleModel, str(entity.bundle_id))
        if row is not None:
            raise ValueError("Evidence bundle idempotency records are append-only.")
        self._session.add(evidence_bundle_record_to_model(entity))
        try:
            self._session.flush()
        except IntegrityError as exc:
            if _is_evidence_bundle_key_race(exc):
                raise EvidenceBundleKeyRaceError(
                    "Evidence bundle idempotency key was inserted concurrently."
                ) from exc
            raise

    def get_by_key(
        self,
        *,
        project_id: UUID,
        created_by: str,
        idempotency_key: str,
    ) -> EvidenceBundleRecord | None:
        self._session.flush()
        row = self._session.scalars(
            select(EvidenceBundleModel).where(
                EvidenceBundleModel.project_id == str(project_id),
                EvidenceBundleModel.created_by == created_by,
                EvidenceBundleModel.idempotency_key == idempotency_key,
            )
        ).one_or_none()
        return evidence_bundle_record_from_model(row) if row is not None else None
