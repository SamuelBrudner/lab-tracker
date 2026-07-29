from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

from lab_tracker.local_store_locator import PortableStorePath
from lab_tracker.outbound_http import RegisteredHttpPrefix


@pytest.mark.parametrize(
    ("value", "origin", "components", "canonical_url"),
    (
        (
            "https://files.example",
            "https://files.example",
            (),
            "https://files.example/",
        ),
        (
            "https://FILES.example:443/",
            "https://files.example",
            (),
            "https://files.example/",
        ),
        (
            "http://files.example:80",
            "http://files.example",
            (),
            "http://files.example/",
        ),
        (
            "https://files.example:8443/artifacts",
            "https://files.example:8443",
            ("artifacts",),
            "https://files.example:8443/artifacts/",
        ),
        (
            "https://bücher.example/%CE%B1/",
            "https://xn--bcher-kva.example",
            ("α",),
            "https://xn--bcher-kva.example/%CE%B1/",
        ),
    ),
)
def test_registered_http_prefix_normalizes_origin_and_directory_form(
    value: str,
    origin: str,
    components: tuple[str, ...],
    canonical_url: str,
) -> None:
    prefix = RegisteredHttpPrefix.parse(value)

    assert prefix is not None
    assert prefix.origin == origin
    assert prefix.path_components == components
    assert prefix.canonical_url == canonical_url


@pytest.mark.parametrize(
    "value",
    (
        "",
        "files.example/artifacts",
        "ftp://files.example/artifacts",
        "https:///artifacts",
        "https://user:secret@files.example/artifacts",
        "https://files.example/artifacts?token=secret",
        "https://files.example/artifacts?",
        "https://files.example/artifacts#fragment",
        "https://files.example/artifacts#",
        "https://files.example/.",
        "https://files.example/..",
        "https://files.example/%2E",
        "https://files.example/%2e%2e",
        "https://files.example/art%2Eifacts",
        "https://files.example/artifacts/%2Fsecret",
        "https://files.example/artifacts/%5Csecret",
        "https://files.example/artifacts\\secret",
        "https://files.example//artifacts",
        "https://files.example/artifacts//",
        "https://files.example/artifacts/%",
        "https://files.example/artifacts/%2",
        "https://files.example/artifacts/%GG",
        "https://files.example/artifacts/%FF",
        "https://files.example/artifacts\nsecret",
        "https://files.example/artifacts/%00secret",
        "https://files.example/artifacts;version=1",
        "https://files.example/artifacts%3Bversion%3D1",
        "https://files.example/artifacts/%3Fquery",
        "https://files.example/artifacts/%23fragment",
        "https://files.example/artifacts/%25escape",
        "https://files.example/%61rtifacts",
        "https://files.example/..%EF%BC%8Fsecret",
        "https://files.example/%EF%BC%8E%EF%BC%8E/secret",
        "https://files.example/%EF%BC%852e%EF%BC%852e/secret",
        "https://files.example/artifacts%EF%BC%9Bversion%3D1",
        "https://files.example/α",
        "https://files.example/trailing.",
        "https://files.example/trailing%20",
        "https://files.example/CON",
        "https://files.example/aux.txt",
        "https://files.example/name%3Astream",
    ),
)
def test_registered_http_prefix_rejects_ambiguous_or_nonportable_bases(
    value: str,
) -> None:
    assert RegisteredHttpPrefix.parse(value) is None


def test_registered_http_prefix_enforces_shared_component_and_path_bounds() -> None:
    oversized_component = "a" * 256
    long_components = "/".join(["a" * 200] * 21)

    assert (
        RegisteredHttpPrefix.parse(
            f"https://files.example/{oversized_component}"
        )
        is None
    )
    assert (
        RegisteredHttpPrefix.parse(f"https://files.example/{long_components}")
        is None
    )


def test_registered_http_prefix_is_immutable_and_constructor_checked() -> None:
    prefix = RegisteredHttpPrefix.parse("https://files.example/artifacts")
    assert prefix is not None

    assert "canonical_url" not in {field.name for field in fields(prefix)}
    assert prefix.canonical_url == "https://files.example/artifacts/"
    with pytest.raises(FrozenInstanceError):
        prefix.origin = "https://attacker.example"  # type: ignore[misc]
    with pytest.raises(ValueError, match="prefix is invalid"):
        RegisteredHttpPrefix(
            origin="https://FILES.example",
            path_components=("artifacts",),
        )
    with pytest.raises(ValueError, match="prefix is invalid"):
        RegisteredHttpPrefix(
            origin="https://files.example",
            path_components=("artifacts;version=1",),
        )


def test_compose_encodes_a_portable_locator_once_beneath_the_prefix() -> None:
    prefix = RegisteredHttpPrefix.parse("https://files.example/artifacts")
    locator = PortableStorePath.parse_decoded("nested/α artifact.bin")
    assert prefix is not None
    assert locator is not None

    assert prefix.compose(locator) == (
        "https://files.example/artifacts/nested/%CE%B1%20artifact.bin"
    )


def test_root_prefix_forms_compose_the_same_canonical_url() -> None:
    without_slash = RegisteredHttpPrefix.parse("https://files.example")
    with_slash = RegisteredHttpPrefix.parse("https://files.example/")
    locator = PortableStorePath.parse_decoded("nested/artifact.bin")
    assert without_slash is not None
    assert with_slash is not None
    assert locator is not None

    assert without_slash == with_slash
    assert without_slash.compose(locator) == (
        "https://files.example/nested/artifact.bin"
    )


def test_compose_rejects_http_path_parameter_ambiguity() -> None:
    prefix = RegisteredHttpPrefix.parse("https://files.example/artifacts")
    literal = PortableStorePath.parse_decoded("nested/artifact;version=1")
    compatibility = PortableStorePath.parse_decoded("nested/artifact；version=1")
    assert prefix is not None
    assert literal is not None
    assert compatibility is not None

    assert prefix.compose(literal) is None
    assert prefix.compose(compatibility) is None


def test_compose_enforces_the_bound_across_prefix_and_locator_together() -> None:
    base = "/".join(["a" * 200] * 11)
    locator_value = "/".join(["b" * 200] * 11)
    prefix = RegisteredHttpPrefix.parse(f"https://files.example/{base}")
    locator = PortableStorePath.parse_decoded(locator_value)
    assert prefix is not None
    assert locator is not None

    assert prefix.compose(locator) is None


@pytest.mark.parametrize(
    "candidate",
    (
        "https://files.example/artifacts",
        "https://files.example/artifacts/",
        "https://FILES.example:443/artifacts/nested/file.bin",
        "https://files.example/artifacts/nested/",
        "https://files.example/artifacts/%CE%B1.bin",
    ),
)
def test_contains_accepts_only_structural_descendants(candidate: str) -> None:
    prefix = RegisteredHttpPrefix.parse("https://files.example/artifacts")
    assert prefix is not None

    assert prefix.contains(candidate)


@pytest.mark.parametrize(
    "candidate",
    (
        "http://files.example/artifacts/file.bin",
        "https://files.example:8443/artifacts/file.bin",
        "https://other.example/artifacts/file.bin",
        "https://files.example/artifacts-archive/file.bin",
        "https://files.example/artifacts%2Farchive/file.bin",
        "https://files.example/artifacts/%2e%2e/secret",
        "https://files.example/artifacts//secret",
        "https://files.example/artifacts/file;version=1",
        "https://files.example/artifacts/file.bin?token=secret",
        "https://files.example/artifacts/%ce%b1.bin",
        "https://files.example/artifacts/%66ile.bin",
        "https://user@files.example/artifacts/file.bin",
    ),
)
def test_contains_rejects_origin_prefix_and_encoding_aliases(candidate: str) -> None:
    prefix = RegisteredHttpPrefix.parse("https://files.example/artifacts")
    assert prefix is not None

    assert not prefix.contains(candidate)


def test_root_prefix_contains_portable_paths_on_only_its_exact_origin() -> None:
    prefix = RegisteredHttpPrefix.parse("https://files.example/")
    assert prefix is not None

    assert prefix.contains("https://files.example/")
    assert prefix.contains("https://files.example/nested/file.bin")
    assert not prefix.contains("http://files.example/nested/file.bin")
    assert not prefix.contains("https://other.example/nested/file.bin")
    assert not prefix.contains("https://files.example/nested/CON")


@pytest.mark.parametrize(
    ("location", "expected"),
    (
        (
            "next.bin",
            "https://files.example/artifacts/nested/next.bin",
        ),
        (
            "/artifacts/final.bin",
            "https://files.example/artifacts/final.bin",
        ),
        (
            "https://FILES.example:443/artifacts/final.bin",
            "https://files.example/artifacts/final.bin",
        ),
        (
            "%CE%B1.bin",
            "https://files.example/artifacts/nested/%CE%B1.bin",
        ),
    ),
)
def test_resolve_redirect_returns_canonical_in_prefix_urls(
    location: str,
    expected: str,
) -> None:
    prefix = RegisteredHttpPrefix.parse("https://files.example/artifacts")
    assert prefix is not None

    assert (
        prefix.resolve_redirect(
            "https://files.example/artifacts/nested/source.bin",
            location,
        )
        == expected
    )


@pytest.mark.parametrize(
    "location",
    (
        "",
        ".",
        "./next.bin",
        "../secret.bin",
        "%2e%2e/secret.bin",
        "/outside/secret.bin",
        "https://other.example/artifacts/secret.bin",
        "//other.example/artifacts/secret.bin",
        "nested//secret.bin",
        "nested\\secret.bin",
        "secret.bin?token=value",
        "secret.bin#fragment",
        "secret;version=1",
        "secret%3Bversion%3D1",
        "secret%EF%BC%9Bversion%3D1",
        "..%EF%BC%8Fsecret.bin",
        "%EF%BC%8E%EF%BC%8E/secret.bin",
        "%EF%BC%852e%EF%BC%852e/secret.bin",
        "%2Fsecret.bin",
        "%5Csecret.bin",
        "%ZZ",
        "%73ecret.bin",
        "CON",
    ),
)
def test_resolve_redirect_rejects_ambiguous_or_escaping_locations(
    location: str,
) -> None:
    prefix = RegisteredHttpPrefix.parse("https://files.example/artifacts")
    assert prefix is not None

    assert (
        prefix.resolve_redirect(
            "https://files.example/artifacts/nested/source.bin",
            location,
        )
        is None
    )


@pytest.mark.parametrize(
    "location",
    (
        "../secret.bin",
        "%2e%2e/secret.bin",
        "//other.example/artifacts/secret.bin",
        "nested//secret.bin",
        "secret.bin?token=value",
        "secret;version=1",
        "%2Fsecret.bin",
        "%ZZ",
    ),
)
def test_invalid_raw_locations_are_rejected_before_urljoin(
    location: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = RegisteredHttpPrefix.parse("https://files.example/artifacts")
    assert prefix is not None
    calls: list[tuple[str, str]] = []

    def unexpected_urljoin(current_url: str, raw_location: str) -> str:
        calls.append((current_url, raw_location))
        raise AssertionError("urljoin received an ambiguous raw Location")

    monkeypatch.setattr(
        "lab_tracker.outbound_http.urljoin",
        unexpected_urljoin,
    )

    assert (
        prefix.resolve_redirect(
            "https://files.example/artifacts/source.bin",
            location,
        )
        is None
    )
    assert calls == []


def test_resolve_redirect_rejects_a_current_url_outside_the_prefix() -> None:
    prefix = RegisteredHttpPrefix.parse("https://files.example/artifacts")
    assert prefix is not None

    assert (
        prefix.resolve_redirect(
            "https://files.example/outside/source.bin",
            "/artifacts/final.bin",
        )
        is None
    )
