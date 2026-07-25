from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from lab_tracker.local_store_locator import (
    LocalStoreLocator,
    canonical_local_store_uri,
    parse_local_store_uri,
)
from lab_tracker.models import ExternalArtifactReference


@pytest.mark.parametrize(
    ("decoded", "components", "uri_path"),
    [
        (
            "experiment/run-01/data.csv",
            ("experiment", "run-01", "data.csv"),
            "experiment/run-01/data.csv",
        ),
        (".hidden/file name.txt", (".hidden", "file name.txt"), ".hidden/file%20name.txt"),
        (
            "Müller/測定/🧪.txt",
            ("Müller", "測定", "🧪.txt"),
            "M%C3%BCller/%E6%B8%AC%E5%AE%9A/%F0%9F%A7%AA.txt",
        ),
        (
            " leading/interior space/x",
            (" leading", "interior space", "x"),
            "%20leading/interior%20space/x",
        ),
        ("a+b/@sample/(raw).bin", ("a+b", "@sample", "(raw).bin"), "a%2Bb/@sample/%28raw%29.bin"),
    ],
)
def test_store_locator_round_trips_canonical_forms(
    decoded: str,
    components: tuple[str, ...],
    uri_path: str,
):
    locator = LocalStoreLocator.parse_decoded(decoded)

    assert locator is not None
    assert locator.components == components
    assert locator.path == decoded
    assert locator.uri_path == uri_path
    assert LocalStoreLocator.parse_uri_path(uri_path) == locator


def test_store_locator_is_frozen_and_requires_tuple_components():
    locator = LocalStoreLocator(("nested", "artifact.txt"))

    with pytest.raises(FrozenInstanceError):
        locator.components = ("changed",)  # type: ignore[misc]
    with pytest.raises(ValueError, match="components are invalid"):
        LocalStoreLocator(["not", "a", "tuple"])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "decoded",
    [
        "",
        "/absolute",
        "trailing/",
        "two//segments",
        ".",
        "..",
        "a/./b",
        "a/../b",
        "percent%name",
        "forward/slash/in/component/",
        r"back\slash",
        "nul\x00byte",
        "line\nbreak",
        "delete\x7fcontrol",
        "c1\x85control",
        "colon:name",
        "question?mark",
        "hash#mark",
        "less<than",
        "greater>than",
        'double"quote',
        "pipe|name",
        "star*name",
        "trailing.",
        "trailing ",
        "nested/trailing.",
        "nested/trailing ",
        "\ud800",
    ],
)
def test_parse_decoded_rejects_nonportable_or_ambiguous_locators(decoded: str):
    assert LocalStoreLocator.parse_decoded(decoded) is None


@pytest.mark.parametrize(
    "device_name",
    [
        "CON",
        "con.txt",
        "CONIN$",
        "conin$.txt",
        "CONOUT$",
        "conout$.txt",
        "PRN.tar.gz",
        "aux",
        "NUL.json",
        "COM1",
        "com9.txt",
        "LPT1",
        "lpt9.txt",
        "COM¹",
        "com².log",
        "COM³.tar.gz",
        "LPT¹",
        "lpt².log",
        "LPT³.tar.gz",
        "NUL .txt",
        "CON .txt",
        "COM1 .log",
        "LPT¹ .dat",
        "nested/CON.txt",
    ],
)
def test_parse_decoded_rejects_dos_device_names_and_extensions(device_name: str):
    assert LocalStoreLocator.parse_decoded(device_name) is None


@pytest.mark.parametrize(
    "allowed",
    [
        "console",
        "conifer.txt",
        "COM0",
        "COM10",
        "LPT0",
        "LPT10",
        ".config",
        "..named",
        "name..part",
        "space inside.txt",
    ],
)
def test_parse_decoded_allows_non_device_and_hidden_names(allowed: str):
    assert LocalStoreLocator.parse_decoded(allowed) is not None


@pytest.mark.parametrize(
    "raw_path",
    [
        "%",
        "%2",
        "%GG",
        "name%4Z",
        "%FF",
        "%C0%AF",
        "%E2%28%A1",
        "%ED%A0%80",
        "%00",
        "%2E",
        "%2e%2E",
        "a/%2E/b",
        "a/%2e%2e/b",
        "encoded%2Fslash",
        "encoded%5Cbackslash",
        "encoded%3Acolon",
        "encoded%3Fquestion",
        "encoded%23hash",
        "encoded%25percent",
        "double%252Fencoded",
        "double%252e%252eencoded",
        "/leading",
        "trailing/",
        "two//segments",
    ],
)
def test_parse_uri_path_rejects_malformed_or_decoded_aliases(raw_path: str):
    assert LocalStoreLocator.parse_uri_path(raw_path) is None


def test_parse_uri_path_decodes_utf8_strictly_and_canonicalizes_once():
    locator = LocalStoreLocator.parse_uri_path("M%c3%bcller/file%20name/%61.txt")

    assert locator is not None
    assert locator.path == "Müller/file name/a.txt"
    assert locator.uri_path == "M%C3%BCller/file%20name/a.txt"


def test_component_limit_is_measured_in_utf8_bytes():
    assert LocalStoreLocator.parse_decoded("a" * 255) is not None
    assert LocalStoreLocator.parse_decoded("a" * 256) is None
    assert LocalStoreLocator.parse_decoded("é" * 127) is not None
    assert LocalStoreLocator.parse_decoded("é" * 128) is None


def test_canonical_uri_path_limit_accepts_4096_bytes():
    decoded = "/".join([*(["a" * 255] * 15), "b" * 254, "c"])

    locator = LocalStoreLocator.parse_decoded(decoded)

    assert locator is not None
    assert len(locator.uri_path.encode("ascii")) == 4096


def test_canonical_uri_path_limit_rejects_more_than_4096_bytes():
    decoded = "/".join([*(["a" * 255] * 15), "b" * 254, "cc"])

    assert LocalStoreLocator.parse_decoded(decoded) is None
    with pytest.raises(ValueError, match="components are invalid"):
        LocalStoreLocator(tuple(decoded.split("/")))


def test_canonical_uri_path_limit_counts_percent_encoded_bytes():
    assert LocalStoreLocator.parse_decoded("/".join(["é" * 127] * 5)) is not None
    assert LocalStoreLocator.parse_decoded("/".join(["é" * 127] * 6)) is None


@pytest.mark.parametrize(
    "store_name",
    [
        "",
        "-leading",
        ".leading",
        "_leading",
        "has space",
        "has/slash",
        "has:port",
        "user@host",
        "é",
        "a" * 64,
    ],
)
def test_canonical_local_store_uri_rejects_invalid_store_names(store_name: str):
    locator = LocalStoreLocator(("artifact.txt",))

    assert canonical_local_store_uri(store_name, locator) is None


@pytest.mark.parametrize(
    "store_name",
    [
        "a",
        "A",
        "lab-store",
        "lab.store_01",
        "a" * 63,
    ],
)
def test_canonical_local_store_uri_accepts_ascii_store_names(store_name: str):
    locator = LocalStoreLocator(("nested", "file name.txt"))

    assert canonical_local_store_uri(store_name, locator) == (
        f"store://{store_name}/nested/file%20name.txt"
    )


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "STORE://lab/path",
        "store:/lab/path",
        "store:lab/path",
        "store:///path",
        "store://lab",
        "store://lab/",
        "store://lab//path",
        "store://lab/path//nested",
        "store://user@lab/path",
        "store://lab:42/path",
        "store://lab/path?query",
        "store://lab/path?",
        "store://lab/path#fragment",
        "store://lab/path#",
        "store://lab/pa\nth",
        "store://lab/pa\rth",
        "store://lab/pa\tth",
        "store://lab/%2Fabsolute",
        "store://lab/%5Cwindows",
        "store://lab/%252e%252e/secret",
        "store://lab/../secret",
    ],
)
def test_parse_local_store_uri_rejects_nonexact_structure(uri: str):
    assert parse_local_store_uri(uri) is None


def test_parse_local_store_uri_returns_canonical_store_locator():
    parsed = parse_local_store_uri(
        "store://lab-store/M%c3%bcller/file%20name/%61.txt"
    )

    assert parsed is not None
    store_name, locator = parsed
    assert store_name == "lab-store"
    assert locator.path == "Müller/file name/a.txt"
    assert canonical_local_store_uri(store_name, locator) == (
        "store://lab-store/M%C3%BCller/file%20name/a.txt"
    )


def test_external_artifact_reference_for_local_store_uses_canonical_fields():
    reference = ExternalArtifactReference.for_local_store(
        store_name="lab-store",
        locator="Müller/file name/a.txt",
        content_hash="sha256:abc",
    )

    assert reference.store_name == "lab-store"
    assert reference.locator == "Müller/file name/a.txt"
    assert reference.uri == "store://lab-store/M%C3%BCller/file%20name/a.txt"
    assert reference.source_system == "store"


@pytest.mark.parametrize(
    ("store_name", "locator"),
    [
        ("-invalid", "path"),
        ("valid", "/absolute"),
        ("valid", "../escape"),
        ("valid", "already%20encoded"),
        ("valid", r"windows\path"),
        ("valid", "CON.txt"),
    ],
)
def test_external_artifact_reference_for_local_store_rejects_invalid_inputs(
    store_name: str,
    locator: str,
):
    with pytest.raises(ValueError, match="Invalid local-store"):
        ExternalArtifactReference.for_local_store(
            store_name=store_name,
            locator=locator,
            content_hash="sha256:abc",
        )


def test_generic_store_reference_keeps_nonlocal_locator_semantics():
    locator = "query:field?raw%value@revision"

    reference = ExternalArtifactReference.for_store(
        store_name="legacy remote",
        locator=locator,
        content_hash="sha256:abc",
    )

    assert reference.uri == f"store://legacy remote/{locator}"
    assert reference.locator == locator


def test_generic_store_reference_preserves_legacy_normalization_and_source_label():
    reference = ExternalArtifactReference.for_store(
        store_name="remote",
        locator=" /nested/path ",
        content_hash="sha256:abc",
        source_system="legacy",
    )

    assert reference.source_system == "legacy"
    assert reference.locator == "nested/path"
    assert reference.uri == "store://remote/nested/path"


def test_direct_external_reference_construction_remains_backward_compatible():
    reference = ExternalArtifactReference(
        source_system="store",
        uri="store://legacy/../path",
        content_hash="sha256:legacy",
        store_name="legacy",
        locator="../path",
    )

    assert reference.locator == "../path"
