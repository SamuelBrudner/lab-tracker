from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from lab_tracker.collection_models import DatasetCollectionSnapshotReference
from lab_tracker.models import (
    DatasetCommitManifest,
    DatasetFile,
    ExternalArtifactKind,
    ExternalArtifactReference,
    OutcomeStatus,
    QuestionLink,
    QuestionLinkRole,
)
from lab_tracker.services.shared import (
    build_commit_manifest,
    compute_commit_hash,
    dataset_manifest_payload,
)

_PRIMARY_QUESTION_ID = UUID("11111111-1111-4111-8111-111111111111")
_SECONDARY_QUESTION_ID = UUID("22222222-2222-4222-8222-222222222222")
_NOTE_ID = UUID("33333333-3333-4333-8333-333333333333")
_SESSION_ID = UUID("44444444-4444-4444-8444-444444444444")


def _primary_link() -> QuestionLink:
    return QuestionLink(
        question_id=_PRIMARY_QUESTION_ID,
        role=QuestionLinkRole.PRIMARY,
    )


@pytest.mark.parametrize(
    ("manifest", "golden_hash"),
    [
        pytest.param(
            DatasetCommitManifest(question_links=[_primary_link()]),
            "623cc1f8b7d0f540bf3842b59a600169232b9d35e70c765d15438a99ce81571f",
            id="empty",
        ),
        pytest.param(
            DatasetCommitManifest(
                files=[
                    DatasetFile(
                        path="a.bin",
                        checksum="sha256:a",
                        size_bytes=999,
                    ),
                    DatasetFile(
                        path="z.bin",
                        checksum="sha256:z",
                        size_bytes=1,
                    ),
                ],
                question_links=[_primary_link()],
            ),
            "911809f0fd452047f16bbf50b259e9ce8e658cae7718b789bf3d10261537ab39",
            id="files",
        ),
        pytest.param(
            DatasetCommitManifest(
                metadata={"z": "last", "a": "first"},
                nwb_metadata={"identifier": "nwb-1"},
                bids_metadata={"subject": "01"},
                question_links=[_primary_link()],
            ),
            "500c6897cd3046011433f2de18fb20c51a8aa659d5be9a46a0d539241ac9af54",
            id="metadata",
        ),
        pytest.param(
            DatasetCommitManifest(
                note_ids=[_NOTE_ID],
                question_links=[
                    _primary_link(),
                    QuestionLink(
                        question_id=_SECONDARY_QUESTION_ID,
                        role=QuestionLinkRole.SECONDARY,
                        outcome_status=OutcomeStatus.SUPPORTS,
                    ),
                ],
                source_session_id=_SESSION_ID,
            ),
            "4e5d30eb0c8da8732f128fe6b583b2e9912e48abb7e53fc540af719e2c7dc503",
            id="lineage",
        ),
        pytest.param(
            DatasetCommitManifest(
                external_artifacts=[
                    ExternalArtifactReference(
                        kind=ExternalArtifactKind.ENTITY,
                        source_system="s3",
                        uri="s3://bucket/run/manifest.json",
                        content_hash="sha256:manifest",
                        metadata={"files": 10_000},
                    )
                ],
                question_links=[_primary_link()],
            ),
            "aabb95d2035942806e012659cfdc1bcbdd417ff717d04734a35d033ef4f5a8aa",
            id="first-class-external-artifact",
        ),
        pytest.param(
            DatasetCommitManifest(
                metadata={
                    "external_artifacts": (
                        '[{"content_hash":"sha256:legacy","kind":"entity",'
                        '"metadata":{},"source_system":"s3",'
                        '"uri":"s3://bucket/legacy.json"}]'
                    )
                },
                question_links=[_primary_link()],
            ),
            "2b3fbfba053a8f77225494a894d9412630c87cfd8e5d5bcafb69e349fff107ff",
            id="legacy-metadata-external-artifact",
        ),
        pytest.param(
            DatasetCommitManifest(
                files=[
                    DatasetFile(
                        path="data.bin",
                        checksum="sha256:data",
                        size_bytes=2**40,
                    )
                ],
                external_artifacts=[
                    ExternalArtifactReference(
                        source_system="datalad",
                        uri="ria+file:///store#dataset",
                        content_hash="sha256:dataset",
                    )
                ],
                metadata={"source": "legacy"},
                nwb_metadata={"session": "baseline"},
                bids_metadata={"task": "odor"},
                note_ids=[_NOTE_ID],
                question_links=[
                    _primary_link(),
                    QuestionLink(
                        question_id=_SECONDARY_QUESTION_ID,
                        role=QuestionLinkRole.SECONDARY,
                        outcome_status=OutcomeStatus.INCONCLUSIVE,
                    ),
                ],
                source_session_id=_SESSION_ID,
            ),
            "865d1cf17408bb911a9cbf63fe05758619f0911f26dbd0d0cefb186b7f0fd6f1",
            id="comprehensive",
        ),
    ],
)
def test_legacy_dataset_manifest_hashes_remain_golden(
    manifest: DatasetCommitManifest,
    golden_hash: str,
) -> None:
    assert manifest.collection_snapshots == []
    assert compute_commit_hash(manifest) == golden_hash


def test_same_collection_key_from_distinct_collections_is_valid_and_hash_sorted() -> None:
    observed_at = datetime(2026, 7, 24, tzinfo=timezone.utc)
    first = DatasetCollectionSnapshotReference(
        snapshot_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        collection_id=UUID("11111111-aaaa-4111-8111-111111111111"),
        collection_key="raw",
        manifest_hash="a" * 64,
        member_count=1,
        total_size_bytes=10,
        observed_at=observed_at,
    )
    second = DatasetCollectionSnapshotReference(
        snapshot_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        collection_id=UUID("22222222-bbbb-4222-8222-222222222222"),
        collection_key="raw",
        manifest_hash="b" * 64,
        member_count=2,
        total_size_bytes=20,
        observed_at=observed_at,
    )

    manifest = build_commit_manifest(
        None,
        [_primary_link()],
        collection_snapshots=[second, first],
    )
    reordered = build_commit_manifest(
        None,
        [_primary_link()],
        collection_snapshots=[first, second],
    )

    assert [snapshot.collection_id for snapshot in manifest.collection_snapshots] == [
        first.collection_id,
        second.collection_id,
    ]
    assert dataset_manifest_payload(manifest)["collection_snapshots"] == [
        {"collection_key": "raw", "manifest_hash": "a" * 64},
        {"collection_key": "raw", "manifest_hash": "b" * 64},
    ]
    assert compute_commit_hash(manifest) == compute_commit_hash(reordered)
