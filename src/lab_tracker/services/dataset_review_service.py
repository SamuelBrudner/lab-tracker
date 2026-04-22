"""Dataset review request domain service mixin."""

from __future__ import annotations

from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, Role, require_role
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    DatasetCommitManifestInput,
    DatasetReview,
    DatasetReviewStatus,
    DatasetStatus,
    utc_now,
)
from lab_tracker.services.dataset_service import (
    _load_attached_files,
    _load_dataset_note_targets,
    _merge_unique_ids,
)
from lab_tracker.services.shared import (
    WRITE_ROLES,
    _build_commit_manifest,
    _compute_commit_hash,
    _ensure_dataset_status_transition,
    _ensure_primary_question_active,
    _manifest_input_from_commit,
)


class DatasetReviewServiceMixin:
    def get_dataset_review(self, review_id: UUID) -> DatasetReview:
        return self._get_from_repository_or_store(
            attribute_name="dataset_reviews",
            entity_id=review_id,
            label="Dataset review",
            loader=lambda repository: repository.dataset_reviews.get(review_id),
        )

    def list_dataset_reviews(
        self,
        *,
        dataset_id: UUID | None = None,
        status: DatasetReviewStatus | None = None,
    ) -> list[DatasetReview]:
        repository = self._active_repository()
        if repository is not None and not self._allow_in_memory:
            reviews, _ = repository.query_dataset_reviews(
                dataset_id=dataset_id,
                status=status.value if status is not None else None,
                limit=None,
                offset=0,
            )
            reviews = self._cache_entities(
                "dataset_reviews",
                reviews,
                lambda review: review.review_id,
            )
            reviews.sort(key=lambda review: (review.requested_at, str(review.review_id)))
            return reviews
        reviews = list(self._store.dataset_reviews.values())
        if dataset_id is not None:
            reviews = [review for review in reviews if review.dataset_id == dataset_id]
        if status is not None:
            reviews = [review for review in reviews if review.status == status]
        reviews.sort(key=lambda review: (review.requested_at, str(review.review_id)))
        return reviews

    def request_dataset_review(
        self,
        dataset_id: UUID,
        *,
        reviewer_user_id: UUID | None = None,
        comments: str | None = None,
        actor: AuthContext | None = None,
    ) -> DatasetReview:
        require_role(actor, WRITE_ROLES)
        dataset = self.get_dataset(dataset_id)
        if dataset.status == DatasetStatus.COMMITTED:
            raise ValidationError("Committed datasets cannot be reviewed.")

        pending = self.list_dataset_reviews(
            dataset_id=dataset_id,
            status=DatasetReviewStatus.PENDING,
        )
        if pending:
            return pending[0]

        review = DatasetReview(
            review_id=uuid4(),
            dataset_id=dataset_id,
            reviewer_user_id=reviewer_user_id,
            status=DatasetReviewStatus.PENDING,
            comments=comments,
            requested_at=utc_now(),
            resolved_at=None,
        )
        self._store.dataset_reviews[review.review_id] = review
        self._run_repository_write(lambda repository: repository.dataset_reviews.save(review))
        return review

    def resolve_dataset_review(
        self,
        review_id: UUID,
        *,
        status: DatasetReviewStatus,
        comments: str | None = None,
        actor: AuthContext | None = None,
    ) -> DatasetReview:
        require_role(actor, {Role.ADMIN, Role.EDITOR})
        review = self.get_dataset_review(review_id)
        if review.status != DatasetReviewStatus.PENDING:
            raise ValidationError("Dataset review is already resolved.")
        if status == DatasetReviewStatus.PENDING:
            raise ValidationError("Resolved dataset review status must not be pending.")

        dataset = None
        if status == DatasetReviewStatus.APPROVED:
            dataset = self.get_dataset(review.dataset_id)
            if dataset.status != DatasetStatus.COMMITTED:
                _ensure_dataset_status_transition(dataset.status, DatasetStatus.COMMITTED)
                primary_question = self.get_question(dataset.primary_question_id)
                _ensure_primary_question_active(primary_question)
                base_manifest = _manifest_input_from_commit(dataset.commit_manifest)
                attached_files = _load_attached_files(self, dataset.dataset_id)
                if attached_files is None:
                    files = list(base_manifest.files)
                else:
                    files = attached_files
                if not files:
                    raise ValidationError("At least one file is required to commit a dataset.")

                note_ids = list(base_manifest.note_ids)
                note_targets = _load_dataset_note_targets(self, dataset.dataset_id)
                if note_targets:
                    note_ids = _merge_unique_ids(note_ids, note_targets)

                manifest_for_commit = DatasetCommitManifestInput(
                    files=files,
                    metadata=base_manifest.metadata,
                    nwb_metadata=base_manifest.nwb_metadata,
                    bids_metadata=base_manifest.bids_metadata,
                    note_ids=note_ids,
                    extraction_provenance=base_manifest.extraction_provenance,
                    source_session_id=base_manifest.source_session_id,
                )
                resolved_manifest = _build_commit_manifest(
                    manifest_for_commit,
                    dataset.question_links,
                )
                self._ensure_source_session_valid(
                    resolved_manifest.source_session_id, dataset.project_id
                )
                dataset.commit_manifest = resolved_manifest
                dataset.commit_hash = _compute_commit_hash(resolved_manifest)
                dataset.status = DatasetStatus.COMMITTED
                dataset.updated_at = utc_now()

        review.status = status
        if comments is not None:
            review.comments = comments
        review.resolved_at = utc_now()

        if dataset is not None and dataset.status == DatasetStatus.COMMITTED:
            self._run_repository_write(
                lambda repository: (
                    repository.dataset_reviews.save(review),
                    repository.datasets.save(dataset),
                )
            )
        else:
            self._run_repository_write(lambda repository: repository.dataset_reviews.save(review))

        return review
