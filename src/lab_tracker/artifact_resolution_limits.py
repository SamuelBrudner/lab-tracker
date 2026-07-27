"""Portable bounds for artifact content returned inline.

This module is intentionally free of transport, framework, and model imports so
HTTP, application, and resolver boundaries can validate one identical contract
before any authority check or I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
MAX_INLINE_ARTIFACT_BYTES = DEFAULT_MAX_BYTES
MAX_ARTIFACT_BYTE_OFFSET = 2**53 - 1


class ArtifactContentBoundsError(ValueError):
    """A requested inline artifact view is outside the portable contract."""


@dataclass(frozen=True, slots=True)
class ArtifactContentBounds:
    """A validated maximum and optional half-open artifact byte range."""

    max_bytes: int
    byte_range: tuple[int, int] | None

    def __post_init__(self) -> None:
        """Protect direct construction as strongly as the named factories."""

        self._validate_max_bytes(self.max_bytes)
        if self.byte_range is None:
            return
        if type(self.byte_range) is not tuple or len(self.byte_range) != 2:
            raise ArtifactContentBoundsError(
                "byte_range must be a two-item tuple or None."
            )
        start, end = self.byte_range
        self._validate_offset(start, name="byte_start")
        self._validate_offset(end, name="byte_end")
        if end < start:
            raise ArtifactContentBoundsError(
                "byte_end must be greater than or equal to byte_start."
            )

    @property
    def returned_allowance(self) -> int:
        """Maximum content bytes this selected view may return."""

        if self.byte_range is None:
            return self.max_bytes
        start, end = self.byte_range
        return min(self.max_bytes, end - start)

    @classmethod
    def for_request(
        cls,
        max_bytes: int | None,
        byte_start: int | None,
        byte_end: int | None,
    ) -> ArtifactContentBounds:
        """Validate independent request fields and apply the default byte cap."""

        effective_max_bytes = DEFAULT_MAX_BYTES if max_bytes is None else max_bytes
        cls._validate_max_bytes(effective_max_bytes)

        if (byte_start is None) != (byte_end is None):
            raise ArtifactContentBoundsError(
                "byte_start and byte_end must be provided together."
            )
        if byte_start is None:
            return cls(
                max_bytes=effective_max_bytes,
                byte_range=None,
            )
        assert byte_end is not None  # paired-presence check above narrows at runtime

        cls._validate_offset(byte_start, name="byte_start")
        cls._validate_offset(byte_end, name="byte_end")
        if byte_end < byte_start:
            raise ArtifactContentBoundsError(
                "byte_end must be greater than or equal to byte_start."
            )
        return cls(
            max_bytes=effective_max_bytes,
            byte_range=(byte_start, byte_end),
        )

    @classmethod
    def from_resolver(
        cls,
        max_bytes: int,
        byte_range: tuple[int, int] | None,
    ) -> ArtifactContentBounds:
        """Validate the tuple-shaped view accepted by Python resolver APIs.

        Unlike :meth:`for_request`, this lower-level boundary never interprets
        ``None`` as a request for the default. Resolver callers must pass the
        already-selected exact integer limit explicitly.
        """

        cls._validate_max_bytes(max_bytes)
        if byte_range is None:
            return cls(max_bytes=max_bytes, byte_range=None)
        if type(byte_range) is not tuple or len(byte_range) != 2:
            raise ArtifactContentBoundsError(
                "byte_range must be a two-item tuple or None."
            )
        return cls(max_bytes=max_bytes, byte_range=byte_range)

    @staticmethod
    def _validate_max_bytes(value: object) -> None:
        if type(value) is not int:
            raise ArtifactContentBoundsError("max_bytes must be an integer.")
        if not 1 <= value <= MAX_INLINE_ARTIFACT_BYTES:
            raise ArtifactContentBoundsError(
                "max_bytes must be between 1 and "
                f"{MAX_INLINE_ARTIFACT_BYTES}, inclusive."
            )

    @staticmethod
    def _validate_offset(value: object, *, name: str) -> None:
        if type(value) is not int:
            raise ArtifactContentBoundsError(f"{name} must be an integer.")
        if not 0 <= value <= MAX_ARTIFACT_BYTE_OFFSET:
            raise ArtifactContentBoundsError(
                f"{name} must be between 0 and "
                f"{MAX_ARTIFACT_BYTE_OFFSET}, inclusive."
            )
