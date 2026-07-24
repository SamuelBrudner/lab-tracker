"""SSRF-safe outbound HTTP primitives.

The policy in this module separates URL authorization from transport.  A target
is parsed once, every DNS answer is validated, and the approved numeric socket
addresses are carried into a client that never resolves the hostname again.
The logical hostname is retained for the HTTP ``Host`` header, TLS SNI, and
certificate hostname verification.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from urllib.parse import SplitResult, quote, urlsplit

_HTTP_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_MAX_URL_LENGTH = 8192
_STREAM_CHUNK_SIZE = 1024 * 1024
_GENERIC_POLICY_DETAIL = "Outbound HTTP destination is not allowed."
_GENERIC_TRANSPORT_DETAIL = "Outbound HTTP request failed."
_PATH_SAFE_CHARACTERS = "/:@!$&'()*+,;=-._~%"
_QUERY_SAFE_CHARACTERS = "/?:@!$&'()*+,;=-._~%"

# IPv6 transition mechanisms can make an apparently public IPv6 literal encode
# a different IPv4 destination.  They are unnecessary for artifact resolution,
# so the public policy rejects them rather than trying to reason about a local
# relay's behavior.
_NAT64_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)
_INTERNAL_ELIGIBLE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fec0::/10"),
)


class OutboundHttpPolicyError(ValueError):
    """A destination cannot be authorized without widening the egress policy."""


class OutboundHttpTransportError(RuntimeError):
    """A vetted HTTP request failed without exposing target or network details."""


class _AddressScope(Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class ApprovedSocketAddress:
    """One numeric address approved for a single outbound connection."""

    family: int
    socktype: int
    protocol: int
    sockaddr: tuple[object, ...]
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address

    @classmethod
    def from_ip(cls, value: str, port: int) -> ApprovedSocketAddress:
        """Build a TCP socket address from an IP literal for tests and adapters."""

        _validate_port(port)
        parsed = ipaddress.ip_address(value)
        if isinstance(parsed, ipaddress.IPv4Address):
            return cls(
                family=socket.AF_INET,
                socktype=socket.SOCK_STREAM,
                protocol=socket.IPPROTO_TCP,
                sockaddr=(str(parsed), port),
                ip=parsed,
            )
        return cls(
            family=socket.AF_INET6,
            socktype=socket.SOCK_STREAM,
            protocol=socket.IPPROTO_TCP,
            sockaddr=(str(parsed), port, 0, 0),
            ip=parsed,
        )


@dataclass(frozen=True)
class ApprovedHttpTarget:
    """A canonical HTTP target bound to already-vetted numeric addresses."""

    scheme: str
    hostname: str
    port: int
    request_target: str
    absolute_url: str
    origin: str
    addresses: tuple[ApprovedSocketAddress, ...]

    @property
    def host_header(self) -> str:
        host = _url_host(self.hostname)
        if self.port == _DEFAULT_PORTS[self.scheme]:
            return host
        return f"{host}:{self.port}"


class AddressResolver(Protocol):
    """Resolve one logical hostname without making a connection."""

    def resolve(self, hostname: str, port: int) -> Sequence[ApprovedSocketAddress]: ...


class SystemAddressResolver:
    """Resolve TCP addresses with the operating system resolver."""

    def resolve(self, hostname: str, port: int) -> Sequence[ApprovedSocketAddress]:
        answers = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        return tuple(_approved_address_from_getaddrinfo(answer, port) for answer in answers)


class OutboundHttpPolicy:
    """Authorize public HTTP(S) targets plus tightly scoped internal exceptions.

    Public targets are allowed only when *every* resolved address is globally
    routable.  Internal targets require a conjunctive exception: an exact
    normalized origin in ``allowed_authorities`` and every answer inside one of
    ``allowed_networks``.  Supplying only half of that configuration is invalid.
    """

    def __init__(
        self,
        *,
        address_resolver: AddressResolver | None = None,
        allowed_authorities: Sequence[str] = (),
        allowed_networks: Sequence[str] = (),
    ) -> None:
        if bool(allowed_authorities) != bool(allowed_networks):
            raise ValueError(
                "Internal HTTP exceptions require both exact authorities and networks."
            )
        self._address_resolver = address_resolver or SystemAddressResolver()
        configured_authorities = tuple(allowed_authorities)
        configured_networks = tuple(allowed_networks)
        self._allowed_origins = frozenset(
            _parse_configured_origin(value) for value in configured_authorities
        )
        try:
            self._allowed_networks = tuple(
                ipaddress.ip_network(value.strip(), strict=True) for value in configured_networks
            )
        except ValueError as exc:
            raise ValueError("Invalid internal HTTP network configuration.") from exc
        if len(self._allowed_origins) != len(configured_authorities):
            raise ValueError("Duplicate internal HTTP authorities are not allowed.")
        if len(set(self._allowed_networks)) != len(configured_networks):
            raise ValueError("Duplicate internal HTTP networks are not allowed.")

    def authorize(self, url: str) -> ApprovedHttpTarget:
        """Parse, resolve, and authorize one URL without sending request bytes."""

        parsed = _parse_http_url(url)
        is_internal_exception = parsed.origin in self._allowed_origins
        if _is_local_hostname(parsed.hostname) and not is_internal_exception:
            raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL)

        literal = _ip_literal(parsed.hostname)
        if literal is None:
            try:
                raw_addresses = self._address_resolver.resolve(parsed.hostname, parsed.port)
            except OSError:
                raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL) from None
        else:
            raw_addresses = (ApprovedSocketAddress.from_ip(str(literal), parsed.port),)

        addresses = _validate_and_deduplicate_addresses(raw_addresses, parsed.port)
        scopes = {_address_scope(address) for address in addresses}
        if _AddressScope.FORBIDDEN in scopes or len(scopes) != 1:
            raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL)
        policy_addresses = tuple(_policy_ip(address.ip) for address in addresses)
        if is_internal_exception:
            if not all(
                _inside_one_network(address, self._allowed_networks) for address in policy_addresses
            ):
                raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL)
        elif scopes != {_AddressScope.PUBLIC}:
            raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL)

        return ApprovedHttpTarget(
            scheme=parsed.scheme,
            hostname=parsed.hostname,
            port=parsed.port,
            request_target=parsed.request_target,
            absolute_url=parsed.absolute_url,
            origin=parsed.origin,
            addresses=addresses,
        )


class PinnedSocketConnector(Protocol):
    """Open a socket using only the addresses approved by the policy."""

    def connect(
        self, addresses: Sequence[ApprovedSocketAddress], timeout: float
    ) -> socket.socket: ...


class SystemPinnedSocketConnector:
    """Connect directly to vetted numeric sockaddrs under one shared deadline."""

    def connect(self, addresses: Sequence[ApprovedSocketAddress], timeout: float) -> socket.socket:
        deadline = time.monotonic() + timeout
        for address in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            candidate: socket.socket | None = None
            try:
                candidate = socket.socket(address.family, address.socktype, address.protocol)
                candidate.settimeout(remaining)
                candidate.connect(address.sockaddr)
                peer = candidate.getpeername()
                peer_ip = ipaddress.ip_address(str(peer[0]).split("%", 1)[0])
                peer_port = int(peer[1])
                if peer_ip != address.ip or peer_port != address.sockaddr[1]:
                    candidate.close()
                    continue
                return candidate
            except (OSError, ValueError):
                if candidate is not None:
                    candidate.close()
        raise OutboundHttpTransportError(_GENERIC_TRANSPORT_DETAIL)


class OutboundHttpResponse(Protocol):
    """Narrow streaming response consumed by artifact and health adapters."""

    status_code: int

    def get_header(self, name: str) -> str | None: ...

    def iter_bytes(self) -> Iterable[bytes]: ...

    def close(self) -> None: ...

    def __enter__(self) -> OutboundHttpResponse: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None: ...


class OutboundHttpClient(Protocol):
    """Perform one request to an already-approved target without redirects."""

    def open(self, method: str, target: ApprovedHttpTarget) -> OutboundHttpResponse: ...


class _StreamingResponse:
    def __init__(
        self,
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
    ) -> None:
        self._connection = connection
        self._response = response
        self.status_code = response.status

    def get_header(self, name: str) -> str | None:
        return self._response.getheader(name)

    def iter_bytes(self) -> Iterator[bytes]:
        try:
            while True:
                chunk = self._response.read(_STREAM_CHUNK_SIZE)
                if not chunk:
                    return
                yield chunk
        except (OSError, http.client.HTTPException):
            raise OutboundHttpTransportError(_GENERIC_TRANSPORT_DETAIL) from None

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()

    def __enter__(self) -> _StreamingResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        self.close()


class _PinnedHttpConnection(http.client.HTTPConnection):
    def __init__(
        self,
        target: ApprovedHttpTarget,
        connector: PinnedSocketConnector,
        timeout: float,
    ) -> None:
        super().__init__(target.hostname, target.port, timeout=timeout)
        self._approved_addresses = target.addresses
        self._connector = connector
        self._pinned_timeout = timeout

    def connect(self) -> None:
        self.sock = self._connector.connect(self._approved_addresses, self._pinned_timeout)


class _PinnedHttpsConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        target: ApprovedHttpTarget,
        connector: PinnedSocketConnector,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(
            target.hostname,
            target.port,
            timeout=timeout,
            context=context,
        )
        self._approved_addresses = target.addresses
        self._connector = connector
        self._pinned_timeout = timeout
        self._pinned_ssl_context = context

    def connect(self) -> None:
        raw_socket = self._connector.connect(self._approved_addresses, self._pinned_timeout)
        try:
            # ``self.host`` is the canonical logical hostname, not the numeric
            # address, preserving both SNI and certificate hostname checks.
            self.sock = self._pinned_ssl_context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


class SafeHttpClient:
    """One-hop HTTP client that can connect only to an approved target."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        connector: PinnedSocketConnector | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("HTTP timeout must be positive.")
        self._timeout = timeout
        self._connector = connector or SystemPinnedSocketConnector()
        self._ssl_context = ssl_context or ssl.create_default_context()

    def open(self, method: str, target: ApprovedHttpTarget) -> OutboundHttpResponse:
        normalized_method = method.upper()
        if normalized_method not in {"GET", "HEAD"}:
            raise ValueError("SafeHttpClient supports only GET and HEAD.")
        connection: http.client.HTTPConnection
        if target.scheme == "https":
            connection = _PinnedHttpsConnection(
                target,
                self._connector,
                self._timeout,
                self._ssl_context,
            )
        else:
            connection = _PinnedHttpConnection(target, self._connector, self._timeout)
        try:
            connection.request(
                normalized_method,
                target.request_target,
                headers={
                    "Accept-Encoding": "identity",
                    "User-Agent": "lab-tracker/0.1",
                },
            )
            response = connection.getresponse()
            content_encoding = response.getheader("content-encoding")
            if content_encoding and content_encoding.strip().lower() != "identity":
                response.close()
                connection.close()
                raise OutboundHttpTransportError(_GENERIC_TRANSPORT_DETAIL)
            return _StreamingResponse(connection, response)
        except (
            OSError,
            UnicodeError,
            http.client.HTTPException,
            OutboundHttpTransportError,
        ):
            connection.close()
            raise OutboundHttpTransportError(_GENERIC_TRANSPORT_DETAIL) from None


@dataclass(frozen=True)
class _ParsedHttpUrl:
    scheme: str
    hostname: str
    port: int
    request_target: str
    absolute_url: str
    origin: str


def _parse_http_url(url: str) -> _ParsedHttpUrl:
    if (
        not url
        or len(url) > _MAX_URL_LENGTH
        or "\\" in url
        or any(ord(character) <= 32 or ord(character) == 127 for character in url)
    ):
        raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL)
    try:
        split = urlsplit(url)
        scheme = split.scheme.lower()
        if scheme not in _HTTP_SCHEMES or not split.netloc:
            raise ValueError
        if "@" in split.netloc or split.username is not None or split.password is not None:
            raise ValueError
        raw_hostname = split.hostname
        if not raw_hostname or "%" in raw_hostname:
            raise ValueError
        hostname = _normalize_hostname(raw_hostname)
        parsed_port = split.port
        port = parsed_port if parsed_port is not None else _DEFAULT_PORTS[scheme]
        _validate_port(port)
    except (UnicodeError, ValueError):
        raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL) from None

    try:
        request_target = quote(
            split.path or "/",
            safe=_PATH_SAFE_CHARACTERS,
            encoding="utf-8",
            errors="strict",
        )
        if split.query:
            query = quote(
                split.query,
                safe=_QUERY_SAFE_CHARACTERS,
                encoding="utf-8",
                errors="strict",
            )
            request_target = f"{request_target}?{query}"
    except UnicodeError:
        raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL) from None
    host = _url_host(hostname)
    port_suffix = "" if port == _DEFAULT_PORTS[scheme] else f":{port}"
    origin = f"{scheme}://{host}{port_suffix}"
    absolute_url = f"{origin}{request_target}"
    return _ParsedHttpUrl(
        scheme=scheme,
        hostname=hostname,
        port=port,
        request_target=request_target,
        absolute_url=absolute_url,
        origin=origin,
    )


def _parse_configured_origin(value: str) -> str:
    parsed = _parse_http_url(value.strip())
    split: SplitResult = urlsplit(value.strip())
    if split.path not in {"", "/"} or split.query or split.fragment:
        raise ValueError("Internal HTTP authorities must be exact origins.")
    return parsed.origin


def _normalize_hostname(hostname: str) -> str:
    candidate = hostname.rstrip(".")
    if not candidate:
        raise ValueError
    literal = _ip_literal(candidate)
    if literal is not None:
        return str(literal)
    encoded = candidate.encode("idna").decode("ascii").lower()
    if len(encoded) > 253:
        raise ValueError
    labels = encoded.split(".")
    if any(not _valid_dns_label(label) for label in labels):
        raise ValueError
    return encoded


def _valid_dns_label(label: str) -> bool:
    return (
        1 <= len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(character.isalnum() or character == "-" for character in label)
        and label.isascii()
    )


def _url_host(hostname: str) -> str:
    return f"[{hostname}]" if ":" in hostname else hostname


def _ip_literal(
    hostname: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        return None


def _is_local_hostname(hostname: str) -> bool:
    if _ip_literal(hostname) is not None:
        return False
    return (
        "." not in hostname
        or hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
        or hostname == "home.arpa"
        or hostname.endswith(".home.arpa")
    )


def _validate_port(port: int) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("Port is outside the valid TCP range.")


def _approved_address_from_getaddrinfo(
    answer: tuple[int, int, int, str, tuple[object, ...]], expected_port: int
) -> ApprovedSocketAddress:
    family, socktype, protocol, _canonical_name, sockaddr = answer
    if family not in {socket.AF_INET, socket.AF_INET6}:
        raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL)
    if socktype != socket.SOCK_STREAM or protocol not in {0, socket.IPPROTO_TCP}:
        raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL)
    if len(sockaddr) < 2 or not isinstance(sockaddr[0], str) or not isinstance(sockaddr[1], int):
        raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL)
    try:
        port = sockaddr[1]
        if port != expected_port or "%" in sockaddr[0]:
            raise ValueError
        parsed_ip = ipaddress.ip_address(sockaddr[0])
    except (TypeError, ValueError):
        raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL) from None
    if (family == socket.AF_INET) != isinstance(parsed_ip, ipaddress.IPv4Address):
        raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL)
    if family == socket.AF_INET:
        normalized_sockaddr: tuple[object, ...] = (str(parsed_ip), port)
    else:
        if (
            len(sockaddr) > 2
            and not isinstance(sockaddr[2], int)
            or len(sockaddr) > 3
            and not isinstance(sockaddr[3], int)
        ):
            raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL)
        flowinfo = sockaddr[2] if len(sockaddr) > 2 else 0
        scope_id = sockaddr[3] if len(sockaddr) > 3 else 0
        normalized_sockaddr = (str(parsed_ip), port, flowinfo, scope_id)
    return ApprovedSocketAddress(
        family=int(family),
        socktype=socket.SOCK_STREAM,
        protocol=socket.IPPROTO_TCP,
        sockaddr=normalized_sockaddr,
        ip=parsed_ip,
    )


def _validate_and_deduplicate_addresses(
    raw_addresses: Sequence[ApprovedSocketAddress], expected_port: int
) -> tuple[ApprovedSocketAddress, ...]:
    approved: list[ApprovedSocketAddress] = []
    seen: set[tuple[int, tuple[object, ...]]] = set()
    for address in raw_addresses:
        if not isinstance(address, ApprovedSocketAddress):
            raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL)
        if (
            address.family not in {socket.AF_INET, socket.AF_INET6}
            or address.socktype != socket.SOCK_STREAM
            or address.protocol != socket.IPPROTO_TCP
            or len(address.sockaddr) < 2
            or address.sockaddr[1] != expected_port
        ):
            raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL)
        expected_family = (
            socket.AF_INET if isinstance(address.ip, ipaddress.IPv4Address) else socket.AF_INET6
        )
        if address.family != expected_family or str(address.sockaddr[0]) != str(address.ip):
            raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL)
        key = (address.family, address.sockaddr)
        if key not in seen:
            approved.append(address)
            seen.add(key)
    if not approved:
        raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL)
    return tuple(approved)


def _policy_ip(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _inside_one_network(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    return any(address.version == network.version and address in network for network in networks)


def _address_scope(address: ApprovedSocketAddress) -> _AddressScope:
    raw = address.ip
    if isinstance(raw, ipaddress.IPv6Address) and (
        raw.ipv4_mapped is not None
        or raw.sixtofour is not None
        or raw.teredo is not None
        or any(raw in network for network in _NAT64_NETWORKS)
    ):
        return _AddressScope.FORBIDDEN
    candidate = _policy_ip(raw)
    if _inside_one_network(candidate, _INTERNAL_ELIGIBLE_NETWORKS):
        return _AddressScope.INTERNAL
    if (
        candidate.is_global
        and not candidate.is_private
        and not candidate.is_loopback
        and not candidate.is_link_local
        and not candidate.is_multicast
        and not candidate.is_reserved
        and not candidate.is_unspecified
    ):
        return _AddressScope.PUBLIC
    return _AddressScope.FORBIDDEN
