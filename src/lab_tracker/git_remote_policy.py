"""Structural authorization for Git remotes used by server-side subprocesses.

Git accepts several URL dialects whose superficially similar spellings can have
different transport semantics.  This module parses operator grants and
request-supplied remotes into one immutable representation, compares those
representations structurally, and reconstructs the only value callers may pass
to Git.

The policy intentionally accepts a conservative subset of Git's remote syntax.
Local paths and remote-helper forms are never valid server-side destinations.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Literal, NoReturn
from urllib.parse import SplitResult, urlsplit

import idna

DEFAULT_GIT_REMOTE_POLICY_VARIABLE = "LAB_TRACKER_GIT_ALLOWED_REMOTES"

GitRemoteScheme = Literal["https", "ssh", "git"]

_DEFAULT_PORTS: dict[GitRemoteScheme, int] = {
    "https": 443,
    "ssh": 22,
    "git": 9418,
}
_SUPPORTED_SCHEMES = frozenset(_DEFAULT_PORTS)
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_DNS_LABEL = re.compile(r"^[a-z0-9-]+$")
_SSH_USER = re.compile(r"^[A-Za-z0-9._~+-]+$")
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~+@-]+$")
_SCHEME_WITHOUT_AUTHORITY = re.compile(
    r"^(?:https?|ssh|git|file|ftp|ftps):", re.IGNORECASE
)


class GitPathStyle(str, Enum):
    """The path interpretation Git will apply to an approved remote."""

    URL = "url"
    SCP_RELATIVE = "scp-relative"
    SCP_ABSOLUTE = "scp-absolute"


@dataclass(frozen=True)
class ApprovedGitRemote:
    """A parsed remote whose subprocess spelling is reconstructed, not reused."""

    scheme: GitRemoteScheme
    host: str
    effective_port: int
    ssh_user: str | None
    path_style: GitPathStyle
    path_segments: tuple[str, ...]
    host_is_ipv6: bool = False

    @property
    def subprocess_value(self) -> str:
        """Return the canonical remote spelling that may be passed to Git."""

        host = f"[{self.host}]" if self.host_is_ipv6 else self.host
        user = f"{self.ssh_user}@" if self.ssh_user is not None else ""
        path = "/".join(self.path_segments)

        if self.path_style is GitPathStyle.SCP_RELATIVE:
            return f"{user}{host}:{path}"
        if self.path_style is GitPathStyle.SCP_ABSOLUTE:
            return f"{user}{host}:/{path}"

        default_port = _DEFAULT_PORTS[self.scheme]
        port = (
            f":{self.effective_port}"
            if self.effective_port != default_port
            else ""
        )
        return f"{self.scheme}://{user}{host}{port}/{path}"


@dataclass(frozen=True)
class GitRemotePolicy:
    """Immutable, segment-bounded allowlist of structurally parsed Git remotes."""

    _grants: tuple[ApprovedGitRemote, ...] = ()

    @classmethod
    def deny_all(cls) -> GitRemotePolicy:
        """Return a policy with no grants."""

        return cls()

    @classmethod
    def from_config(
        cls,
        raw: str | None,
        *,
        variable: str = DEFAULT_GIT_REMOTE_POLICY_VARIABLE,
    ) -> GitRemotePolicy:
        """Parse a comma-separated operator setting without echoing its values.

        Configuration is deliberately strict: entries are not stripped, empty
        entries are errors, and semantic duplicates after normalization fail
        startup.  Error messages identify only the variable, one-based entry
        index, and a safe category.
        """

        if raw is None or raw == "":
            return cls.deny_all()

        grants: list[ApprovedGitRemote] = []
        seen: set[ApprovedGitRemote] = set()
        for index, entry in enumerate(raw.split(","), start=1):
            if not entry:
                raise _config_error(variable, index, "empty entry")
            try:
                grant = _parse_remote(entry)
            except _RemoteParseError as exc:
                raise _config_error(variable, index, exc.category) from None
            if grant in seen:
                raise _config_error(variable, index, "duplicate normalized grant")
            seen.add(grant)
            grants.append(grant)
        return cls(tuple(grants))

    def authorize(self, candidate: str) -> ApprovedGitRemote | None:
        """Return a canonical approved value, or ``None`` without diagnostics."""

        try:
            parsed = _parse_remote(candidate)
        except _RemoteParseError:
            return None
        if any(_grant_matches(grant, parsed) for grant in self._grants):
            return parsed
        return None


class _RemoteParseError(ValueError):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def _config_error(variable: str, index: int, category: str) -> ValueError:
    return ValueError(f"{variable} entry {index} is invalid ({category}).")


def _reject(category: str) -> NoReturn:
    raise _RemoteParseError(category)


def _parse_remote(raw: str) -> ApprovedGitRemote:
    if not isinstance(raw, str) or not raw:
        _reject("empty remote")
    if any(
        character.isspace()
        or unicodedata.category(character).startswith("C")
        for character in raw
    ):
        _reject("whitespace or control characters")
    if raw.startswith("-"):
        _reject("option-shaped component")
    if "\\" in raw:
        _reject("backslash")
    if "%" in raw:
        _reject("percent escape")
    if "=" in raw:
        _reject("equals sign")
    if "?" in raw or "#" in raw:
        _reject("query or fragment")
    if _WINDOWS_DRIVE.match(raw):
        _reject("local or drive path")
    if raw.startswith(("/", "./", "../", "~/")):
        _reject("local or drive path")

    if "://" in raw:
        return _parse_url_remote(raw)
    if "::" in raw:
        _reject("remote-helper form")
    if _SCHEME_WITHOUT_AUTHORITY.match(raw):
        _reject("scheme without authority")
    return _parse_scp_remote(raw)


def _parse_url_remote(raw: str) -> ApprovedGitRemote:
    try:
        parsed = urlsplit(raw)
    except (UnicodeError, ValueError):
        _reject("malformed authority")

    scheme = parsed.scheme.lower()
    if scheme not in _SUPPORTED_SCHEMES:
        _reject("unsupported transport")
    if not parsed.netloc:
        _reject("malformed authority")
    if parsed.query or parsed.fragment:
        _reject("query or fragment")

    typed_scheme: GitRemoteScheme
    if scheme == "https":
        typed_scheme = "https"
    elif scheme == "ssh":
        typed_scheme = "ssh"
    else:
        typed_scheme = "git"

    username, host, host_is_ipv6, port = _parse_url_authority(
        parsed,
        scheme=typed_scheme,
    )
    segments = _parse_path(parsed.path, allow_root=True)
    return ApprovedGitRemote(
        scheme=typed_scheme,
        host=host,
        effective_port=port,
        ssh_user=username,
        path_style=GitPathStyle.URL,
        path_segments=segments,
        host_is_ipv6=host_is_ipv6,
    )


def _parse_url_authority(
    parsed: SplitResult,
    *,
    scheme: GitRemoteScheme,
) -> tuple[str | None, str, bool, int]:
    try:
        username = parsed.username
        password = parsed.password
        hostname = parsed.hostname
        port = parsed.port
    except (UnicodeError, ValueError):
        _reject("malformed authority or port")

    if password is not None:
        _reject("embedded credentials")
    if scheme != "ssh" and username is not None:
        _reject("embedded credentials")
    if username is not None:
        _validate_ssh_user(username)
    if hostname is None or not hostname:
        _reject("malformed authority")

    authority_without_user = parsed.netloc.rsplit("@", 1)[-1]
    if authority_without_user.endswith(":"):
        _reject("malformed authority or port")
    if authority_without_user.startswith("["):
        closing = authority_without_user.find("]")
        if closing < 0:
            _reject("malformed authority")
        suffix = authority_without_user[closing + 1 :]
        if suffix and not suffix.startswith(":"):
            _reject("malformed authority")
        bracketed = True
    else:
        if authority_without_user.count(":") > 1:
            _reject("malformed authority")
        bracketed = False

    host, host_is_ipv6 = _canonical_host(hostname)
    if bracketed and not host_is_ipv6:
        _reject("malformed authority")
    if host_is_ipv6 and not bracketed:
        _reject("malformed authority")

    effective_port = _DEFAULT_PORTS[scheme] if port is None else port
    if effective_port < 1 or effective_port > 65535:
        _reject("malformed authority or port")
    return username, host, host_is_ipv6, effective_port


def _parse_scp_remote(raw: str) -> ApprovedGitRemote:
    prefix, separator, path = raw.partition(":")
    if not separator or not prefix or not path:
        _reject("unsupported or local form")
    if "/" in prefix:
        _reject("unsupported or local form")
    if "[" in prefix or "]" in prefix:
        _reject("bracketed scp host")
    if prefix.count("@") > 1:
        _reject("malformed authority")

    if "@" in prefix:
        username, hostname = prefix.split("@", 1)
        _validate_ssh_user(username)
    else:
        username, hostname = None, prefix
    if not hostname:
        _reject("malformed authority")

    host, host_is_ipv6 = _canonical_host(hostname)
    if host_is_ipv6:
        _reject("bracketed scp host")

    absolute = path.startswith("/")
    segments = _parse_path(path, allow_root=False)
    return ApprovedGitRemote(
        scheme="ssh",
        host=host,
        effective_port=_DEFAULT_PORTS["ssh"],
        ssh_user=username,
        path_style=(
            GitPathStyle.SCP_ABSOLUTE if absolute else GitPathStyle.SCP_RELATIVE
        ),
        path_segments=segments,
        host_is_ipv6=False,
    )


def _validate_ssh_user(username: str) -> None:
    if not username or username.startswith("-") or not _SSH_USER.fullmatch(username):
        _reject("invalid or option-shaped SSH user")


def _canonical_host(raw_host: str) -> tuple[str, bool]:
    if raw_host.startswith("-"):
        _reject("option-shaped component")
    address = _ip_address_or_none(raw_host)
    if address is not None:
        return address.compressed.lower(), isinstance(address, ipaddress.IPv6Address)

    if ":" in raw_host or _looks_like_noncanonical_ip(raw_host):
        _reject("malformed IP address")

    if raw_host.endswith("."):
        _reject("malformed hostname")
    host = unicodedata.normalize("NFC", raw_host).lower()
    try:
        canonical = idna.encode(
            host,
            strict=True,
            uts46=False,
            std3_rules=True,
            transitional=False,
        ).decode("ascii")
    except (UnicodeError, ValueError, idna.IDNAError):
        _reject("malformed hostname")
    address = _ip_address_or_none(canonical)
    if address is not None:
        return address.compressed.lower(), isinstance(address, ipaddress.IPv6Address)
    if _looks_like_noncanonical_ip(canonical):
        _reject("malformed IP address")
    if len(canonical) > 253:
        _reject("malformed hostname")
    labels = canonical.split(".")
    if any(
        not label
        or len(label) > 63
        or not _DNS_LABEL.fullmatch(label)
        or label.startswith("-")
        or label.endswith("-")
        for label in labels
    ):
        _reject("malformed hostname")
    try:
        decoded = idna.decode(
            canonical,
            strict=True,
            uts46=False,
            std3_rules=True,
        )
        round_trip = idna.encode(
            decoded,
            strict=True,
            uts46=False,
            std3_rules=True,
            transitional=False,
        ).decode("ascii")
    except (UnicodeError, ValueError, idna.IDNAError):
        _reject("malformed hostname")
    if round_trip != canonical:
        _reject("malformed hostname")
    return canonical, False


def _ip_address_or_none(
    host: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _looks_like_noncanonical_ip(host: str) -> bool:
    if re.fullmatch(r"[0-9.]+", host):
        return True
    labels = host.lower().split(".")
    return bool(labels) and all(
        label.isdigit() or re.fullmatch(r"0x[0-9a-f]+", label)
        for label in labels
    )


def _parse_path(raw_path: str, *, allow_root: bool) -> tuple[str, ...]:
    path = raw_path[1:] if raw_path.startswith("/") else raw_path
    if not path:
        if allow_root:
            return ()
        _reject("empty path")

    segments = tuple(path.split("/"))
    for segment in segments:
        if not segment:
            _reject("empty path segment")
        if segment in {".", ".."}:
            _reject("dot path segment")
        if segment.startswith("-"):
            _reject("option-shaped component")
        if not _PATH_SEGMENT.fullmatch(segment):
            _reject("unsupported path character")
    return segments


def _grant_matches(
    grant: ApprovedGitRemote,
    candidate: ApprovedGitRemote,
) -> bool:
    if (
        grant.scheme != candidate.scheme
        or grant.host != candidate.host
        or grant.effective_port != candidate.effective_port
        or grant.ssh_user != candidate.ssh_user
        or grant.path_style is not candidate.path_style
    ):
        return False
    prefix_length = len(grant.path_segments)
    return candidate.path_segments[:prefix_length] == grant.path_segments
