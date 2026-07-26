from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from lab_tracker.git_store_locator import (
    GitObjectId,
    PinnedGitPath,
    canonical_git_store_uri,
)
from lab_tracker.local_store_locator import PortableStorePath
from lab_tracker.models import ExternalArtifactKind, ExternalArtifactReference

SHA1_OBJECT_ID = "a" * 40
SHA256_OBJECT_ID = "b" * 64


@pytest.mark.parametrize(
    ("value", "object_format"),
    (
        (SHA1_OBJECT_ID, "sha1"),
        (SHA256_OBJECT_ID, "sha256"),
        ("0123456789abcdef0123456789abcdef01234567", "sha1"),
        (
            "0123456789abcdef0123456789abcdef"
            "0123456789abcdef0123456789abcdef",
            "sha256",
        ),
    ),
)
def test_git_object_id_accepts_supported_full_lowercase_values(
    value: str,
    object_format: str,
) -> None:
    object_id = GitObjectId.parse(value)

    assert object_id is not None
    assert object_id.value == value
    assert object_id.object_format == object_format


@pytest.mark.parametrize(
    "value",
    (
        None,
        123,
        "",
        "HEAD",
        "main",
        "v1.2.3",
        "refs/heads/main",
        "a" * 7,
        "a" * 39,
        "a" * 41,
        "a" * 63,
        "a" * 65,
        "A" * 40,
        "a" * 39 + "G",
        "0" * 40,
        "0" * 64,
        f" {SHA1_OBJECT_ID}",
        f"{SHA1_OBJECT_ID} ",
        f"{SHA1_OBJECT_ID}\n",
        f"{SHA1_OBJECT_ID}\x00",
        "refs/heads/*",
        f"{SHA1_OBJECT_ID}:src/model.py",
        f"{SHA1_OBJECT_ID}^",
        f"{SHA1_OBJECT_ID}~1",
        f"{SHA1_OBJECT_ID}..{SHA256_OBJECT_ID}",
        f"+{SHA1_OBJECT_ID}:refs/cache/object",
        "--upload-pack=helper",
    ),
)
def test_git_object_id_rejects_mutable_ambiguous_or_repaired_forms(
    value: object,
) -> None:
    assert GitObjectId.parse(value) is None


def test_git_object_id_constructor_enforces_the_same_invariant() -> None:
    with pytest.raises(ValueError, match="full lowercase nonzero"):
        GitObjectId("A" * 40)


def test_git_object_id_is_frozen_and_slot_backed() -> None:
    object_id = GitObjectId(SHA1_OBJECT_ID)

    with pytest.raises(FrozenInstanceError):
        object_id.value = SHA256_OBJECT_ID  # type: ignore[misc]
    assert not hasattr(object_id, "__dict__")


@pytest.mark.parametrize(
    ("decoded", "path", "uri_path", "object_id"),
    (
        (
            f"src/model.py@{SHA1_OBJECT_ID}",
            "src/model.py",
            "src/model.py",
            SHA1_OBJECT_ID,
        ),
        (
            f"src/@generated/model.py@{SHA256_OBJECT_ID}",
            "src/@generated/model.py",
            "src/@generated/model.py",
            SHA256_OBJECT_ID,
        ),
        (
            f"Müller/測定 file.py@{SHA1_OBJECT_ID}",
            "Müller/測定 file.py",
            "M%C3%BCller/%E6%B8%AC%E5%AE%9A%20file.py",
            SHA1_OBJECT_ID,
        ),
    ),
)
def test_pinned_git_path_parses_decoded_values_and_uses_the_final_literal_at(
    decoded: str,
    path: str,
    uri_path: str,
    object_id: str,
) -> None:
    pin = PinnedGitPath.parse_decoded(decoded)

    assert pin is not None
    assert pin.path.path == path
    assert pin.object_id.value == object_id
    assert pin.locator == decoded
    assert pin.uri_path == f"{uri_path}@{object_id}"


@pytest.mark.parametrize(
    ("raw_uri_path", "decoded_path", "canonical_uri_path"),
    (
        (
            f"src/model.py@{SHA1_OBJECT_ID}",
            "src/model.py",
            "src/model.py",
        ),
        (
            f"src/@generated/model.py@{SHA1_OBJECT_ID}",
            "src/@generated/model.py",
            "src/@generated/model.py",
        ),
        (
            f"M%C3%BCller/%E6%B8%AC%E5%AE%9A%20file.py@{SHA256_OBJECT_ID}",
            "Müller/測定 file.py",
            "M%C3%BCller/%E6%B8%AC%E5%AE%9A%20file.py",
        ),
        (
            f"src/%40generated/model.py@{SHA1_OBJECT_ID}",
            "src/@generated/model.py",
            "src/@generated/model.py",
        ),
    ),
)
def test_pinned_git_path_parses_uri_paths_only_after_the_final_literal_at_split(
    raw_uri_path: str,
    decoded_path: str,
    canonical_uri_path: str,
) -> None:
    pin = PinnedGitPath.parse_uri_path(raw_uri_path)

    assert pin is not None
    assert pin.path.path == decoded_path
    assert pin.uri_path == f"{canonical_uri_path}@{pin.object_id.value}"


@pytest.mark.parametrize(
    "value",
    (
        f"src/model.py%40{SHA1_OBJECT_ID}",
        f"src/model.py%2540{SHA1_OBJECT_ID}",
        f"src/model.py％40{SHA1_OBJECT_ID}",
    ),
)
def test_encoded_or_compatibility_at_alias_is_never_the_pin_delimiter(
    value: str,
) -> None:
    assert PinnedGitPath.parse_uri_path(value) is None


@pytest.mark.parametrize(
    "path",
    (
        "",
        "/absolute.py",
        "trailing/",
        "two//segments.py",
        ".",
        "..",
        "src/./model.py",
        "src/../secret.py",
        r"src\model.py",
        "src/percent%name.py",
        "src/colon:name.py",
        "src/question?.py",
        "src/hash#name.py",
        "src/star*.py",
        "src/pipe|name.py",
        "src/trailing.",
        "src/trailing ",
        "src/CON.py",
        "src/nul\x00name.py",
        "src/line\nbreak.py",
        "．．/secret.py",
        "src＼secret.py",
        "name：stream.py",
        "\ud800",
    ),
)
def test_pinned_git_path_reuses_the_decoded_portable_path_boundary(path: str) -> None:
    assert PinnedGitPath.parse_decoded(f"{path}@{SHA1_OBJECT_ID}") is None


@pytest.mark.parametrize(
    "raw_path",
    (
        "%",
        "%2",
        "%GG",
        "%FF",
        "%C0%AF",
        "%ED%A0%80",
        "%00",
        "%2E",
        "%2e%2E",
        "src/%2e%2e/secret.py",
        "src%2Fmodel.py",
        "src%5Cmodel.py",
        "src%3Astream.py",
        "src%23fragment.py",
        "src%25percent.py",
        "src%EF%BC%BCmodel.py",
        "%EF%BC%8E%EF%BC%8E/secret.py",
        "/leading.py",
        "trailing/",
        "two//segments.py",
    ),
)
def test_pinned_git_path_reuses_strict_uri_decoding(raw_path: str) -> None:
    assert PinnedGitPath.parse_uri_path(f"{raw_path}@{SHA1_OBJECT_ID}") is None


@pytest.mark.parametrize(
    "value",
    (
        None,
        123,
        "",
        "src/model.py",
        f"@{SHA1_OBJECT_ID}",
        "src/model.py@",
        "src/model.py@HEAD",
        f"src/model.py@ {SHA1_OBJECT_ID}",
        f"src/model.py@{SHA1_OBJECT_ID} ",
    ),
)
def test_pinned_git_path_requires_an_exact_path_and_object_id(value: object) -> None:
    assert PinnedGitPath.parse_decoded(value) is None
    assert PinnedGitPath.parse_uri_path(value) is None


def test_pinned_git_path_does_not_count_the_object_id_against_path_bounds() -> None:
    component = "a" * 255

    pin = PinnedGitPath.parse_decoded(f"{component}@{SHA256_OBJECT_ID}")

    assert pin is not None
    assert len(pin.path.path.encode("utf-8")) == 255
    assert pin.locator == f"{component}@{SHA256_OBJECT_ID}"


def test_pinned_git_path_does_not_count_the_object_id_against_total_path_bound() -> None:
    max_path = "/".join([*(["a" * 255] * 15), "b" * 254, "c"])
    assert len(max_path.encode("ascii")) == 4096

    pin = PinnedGitPath.parse_decoded(f"{max_path}@{SHA256_OBJECT_ID}")

    assert pin is not None
    assert len(pin.path.uri_path.encode("ascii")) == 4096
    assert len(pin.uri_path.encode("ascii")) == 4096 + 1 + 64


def test_pinned_git_path_is_frozen_slot_backed_and_validates_direct_inputs() -> None:
    path = PortableStorePath(("src", "model.py"))
    object_id = GitObjectId(SHA1_OBJECT_ID)
    pin = PinnedGitPath(path=path, object_id=object_id)

    with pytest.raises(FrozenInstanceError):
        pin.path = PortableStorePath(("other.py",))  # type: ignore[misc]
    assert not hasattr(pin, "__dict__")
    with pytest.raises(ValueError, match="portable path"):
        PinnedGitPath(path="src/model.py", object_id=object_id)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="immutable object ID"):
        PinnedGitPath(path=path, object_id=SHA1_OBJECT_ID)  # type: ignore[arg-type]


def test_canonical_git_store_uri_uses_canonical_authority_and_path_forms() -> None:
    pin = PinnedGitPath.parse_decoded(
        f"café/@generated file.py@{SHA256_OBJECT_ID}"
    )
    assert pin is not None

    assert canonical_git_store_uri("user@analysis store:one", pin) == (
        "store://user%40analysis%20store%3Aone/"
        f"caf%C3%A9/@generated%20file.py@{SHA256_OBJECT_ID}"
    )


@pytest.mark.parametrize(
    "store_name",
    (
        "",
        " ",
        "has/slash",
        r"has\backslash",
        "has?query",
        "has#fragment",
        "nul\x00name",
        "[not-ip]",
    ),
)
def test_canonical_git_store_uri_rejects_invalid_store_names(
    store_name: str,
) -> None:
    pin = PinnedGitPath.parse_decoded(f"src/model.py@{SHA1_OBJECT_ID}")
    assert pin is not None

    assert canonical_git_store_uri(store_name, pin) is None


def test_canonical_git_store_uri_requires_a_pinned_git_path() -> None:
    assert (
        canonical_git_store_uri(
            "analysis-repo",
            PortableStorePath(("src", "model.py")),  # type: ignore[arg-type]
        )
        is None
    )


def test_external_artifact_reference_for_git_store_preserves_typed_fields() -> None:
    metadata = {"language": "python", "nested": {"generated": True}}

    reference = ExternalArtifactReference.for_git_store(
        store_name="user@analysis store",
        repository_path="Müller/@generated model.py",
        object_id=SHA256_OBJECT_ID,
        content_hash="sha256:" + "c" * 64,
        kind=ExternalArtifactKind.ACTIVITY,
        metadata=metadata,
    )

    assert reference.kind is ExternalArtifactKind.ACTIVITY
    assert reference.source_system == "store"
    assert reference.store_name == "user@analysis store"
    assert reference.locator == (
        f"Müller/@generated model.py@{SHA256_OBJECT_ID}"
    )
    assert reference.uri == (
        "store://user%40analysis%20store/"
        f"M%C3%BCller/@generated%20model.py@{SHA256_OBJECT_ID}"
    )
    assert reference.metadata == metadata
