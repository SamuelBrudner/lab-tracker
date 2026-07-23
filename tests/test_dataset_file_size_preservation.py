from uuid import UUID, uuid4

import pytest

from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    AcquisitionOutput,
    DatasetCommitManifestInput,
    DatasetFile,
    QuestionLink,
    QuestionLinkRole,
)
from lab_tracker.services.dataset_service import (
    _merge_manifest_files_with_attached_files,
)
from lab_tracker.services.shared import (
    _merge_acquisition_outputs,
    build_commit_manifest,
    compute_commit_hash,
    dataset_manifest_payload,
)

_QUESTION_LINK = QuestionLink(
    question_id=UUID("11111111-1111-1111-1111-111111111111"),
    role=QuestionLinkRole.PRIMARY,
)


def _manifest_file(size_bytes: int | None) -> DatasetCommitManifestInput:
    return DatasetCommitManifestInput(
        files=[
            DatasetFile(
                path=" data/file.bin ",
                checksum=" sha256:abc123 ",
                size_bytes=size_bytes,
            )
        ]
    )


def _acquisition_output(
    size_bytes: int | None,
    *,
    checksum: str = "sha256:abc123",
) -> AcquisitionOutput:
    return AcquisitionOutput(
        output_id=uuid4(),
        session_id=uuid4(),
        file_path="data/file.bin",
        checksum=checksum,
        size_bytes=size_bytes,
    )


def test_manifest_normalization_preserves_size_and_discards_caller_file_id():
    caller_file_id = uuid4()
    manifest = build_commit_manifest(
        DatasetCommitManifestInput(
            files=[
                DatasetFile(
                    file_id=caller_file_id,
                    path=" data/file.bin ",
                    checksum=" sha256:abc123 ",
                    size_bytes=12,
                ),
                DatasetFile(
                    path="legacy/file.bin",
                    checksum="sha256:legacy",
                    size_bytes=None,
                ),
            ]
        ),
        [_QUESTION_LINK],
    )

    assert manifest.files == [
        DatasetFile(
            path="data/file.bin",
            checksum="sha256:abc123",
            size_bytes=12,
        ),
        DatasetFile(
            path="legacy/file.bin",
            checksum="sha256:legacy",
            size_bytes=None,
        ),
    ]
    assert manifest.files[0].file_id is None
    assert manifest.files[1].size_bytes is None


def test_manifest_normalization_rejects_negative_size():
    with pytest.raises(ValidationError, match="file.size_bytes must not be negative"):
        build_commit_manifest(_manifest_file(-1), [_QUESTION_LINK])


def test_file_size_is_provenance_only_and_does_not_change_commit_hash():
    legacy_manifest = build_commit_manifest(
        DatasetCommitManifestInput(
            files=[
                DatasetFile(
                    path=" data/file.bin ",
                    checksum=" sha256:abc123 ",
                )
            ],
            metadata={" run ": " 7 "},
        ),
        [_QUESTION_LINK],
    )
    sized_manifest = legacy_manifest.model_copy(
        update={"files": [legacy_manifest.files[0].model_copy(update={"size_bytes": 123_456})]}
    )

    expected_hash = "0ca5e52a075de59a2d2d1869a9b05a7f784e0216779b50c8601e447c04a9f4fb"
    assert compute_commit_hash(legacy_manifest) == expected_hash
    assert compute_commit_hash(sized_manifest) == expected_hash
    assert dataset_manifest_payload(sized_manifest)["files"] == [
        {"path": "data/file.bin", "checksum": "sha256:abc123"}
    ]


@pytest.mark.parametrize(
    ("manifest_size", "output_size", "expected_size"),
    [
        (None, 12, 12),
        (12, None, 12),
        (12, 12, 12),
    ],
)
def test_acquisition_output_merge_reconciles_compatible_sizes(
    manifest_size: int | None,
    output_size: int | None,
    expected_size: int,
):
    merged = _merge_acquisition_outputs(
        _manifest_file(manifest_size),
        [_acquisition_output(output_size)],
    )

    assert isinstance(merged, DatasetCommitManifestInput)
    assert merged.files == [
        DatasetFile(
            path="data/file.bin",
            checksum="sha256:abc123",
            size_bytes=expected_size,
        )
    ]


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (_acquisition_output(13), "Acquisition output size conflict"),
        (
            _acquisition_output(12, checksum="sha256:different"),
            "Acquisition output checksum conflict",
        ),
    ],
)
def test_acquisition_output_merge_rejects_conflicting_metadata(
    output: AcquisitionOutput,
    message: str,
):
    with pytest.raises(ValidationError, match=message):
        _merge_acquisition_outputs(_manifest_file(12), [output])


def test_acquisition_output_merge_rejects_negative_size():
    with pytest.raises(ValidationError, match="file.size_bytes must not be negative"):
        _merge_acquisition_outputs(None, [_acquisition_output(-1)])


@pytest.mark.parametrize(
    ("manifest_size", "attached_size", "expected_size"),
    [
        (None, 12, 12),
        (12, None, 12),
        (12, 12, 12),
    ],
)
def test_attached_file_merge_reconciles_compatible_sizes(
    manifest_size: int | None,
    attached_size: int | None,
    expected_size: int,
):
    attached_file_id = uuid4()
    merged = _merge_manifest_files_with_attached_files(
        _manifest_file(manifest_size).files,
        [
            DatasetFile(
                file_id=attached_file_id,
                path="data/file.bin",
                checksum="sha256:abc123",
                size_bytes=attached_size,
            )
        ],
    )

    assert merged == [
        DatasetFile(
            path="data/file.bin",
            checksum="sha256:abc123",
            size_bytes=expected_size,
        )
    ]
    assert merged[0].file_id is None


@pytest.mark.parametrize(
    ("attached_file", "message"),
    [
        (
            DatasetFile(
                path="data/file.bin",
                checksum="sha256:abc123",
                size_bytes=13,
            ),
            "Attached file size conflict",
        ),
        (
            DatasetFile(
                path="data/file.bin",
                checksum="sha256:different",
                size_bytes=12,
            ),
            "Attached file checksum conflict",
        ),
    ],
)
def test_attached_file_merge_rejects_conflicting_metadata(
    attached_file: DatasetFile,
    message: str,
):
    with pytest.raises(ValidationError, match=message):
        _merge_manifest_files_with_attached_files(
            _manifest_file(12).files,
            [attached_file],
        )


def test_attached_file_merge_rejects_negative_size():
    with pytest.raises(ValidationError, match="file.size_bytes must not be negative"):
        _merge_manifest_files_with_attached_files(
            [],
            [
                DatasetFile(
                    path="data/file.bin",
                    checksum="sha256:abc123",
                    size_bytes=-1,
                )
            ],
        )
