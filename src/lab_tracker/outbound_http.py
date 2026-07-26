"""SSRF-safe outbound HTTP primitives.

The policy in this module separates URL authorization from transport.  A target
is parsed once, every DNS answer is validated, and the approved numeric socket
addresses are carried into a client that never resolves the hostname again.
The logical hostname is retained for the HTTP ``Host`` header, TLS SNI, and
certificate hostname verification.
"""

from __future__ import annotations

import asyncio
import http.client
import io
import ipaddress
import math
import socket
import ssl
import time
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Protocol, cast
from urllib.parse import SplitResult, quote, urljoin, urlsplit

import dns.asyncresolver
import dns.exception

from lab_tracker.local_store_locator import PortableStorePath

_HTTP_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_MAX_URL_LENGTH = 8192
_STREAM_CHUNK_SIZE = 1024 * 1024
DEFAULT_OUTBOUND_HTTP_DEADLINE_SECONDS = 30.0
MAX_OUTBOUND_HTTP_DEADLINE_SECONDS = 86_400.0
DEFAULT_MAX_HTTP_REDIRECTS = 3
HTTP_REDIRECT_STATUS_CODES: Final = frozenset({301, 302, 303, 307, 308})
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


class OutboundHttpDeadlineExceeded(OutboundHttpTransportError):
    """The request-wide outbound HTTP budget expired."""


def resolve_direct_http_redirect(
    current_url: str,
    location: str,
) -> str | None:
    """Resolve one direct-reference redirect without permitting TLS downgrade.

    This helper does not authorize the resulting destination. Callers must pass
    every returned URL through :class:`OutboundHttpPolicy` before opening a
    connection, including cross-origin redirects.
    """

    try:
        next_url = urljoin(current_url, location)
        current_scheme = urlsplit(current_url).scheme.lower()
        next_scheme = urlsplit(next_url).scheme.lower()
    except (TypeError, UnicodeError, ValueError):
        return None
    if current_scheme == "https" and next_scheme == "http":
        return None
    return next_url


Clock = Callable[[], float]


@dataclass(frozen=True)
class OutboundHttpDeadline:
    """One immutable monotonic deadline shared by every phase of a request."""

    expires_at: float
    clock: Clock = field(default=time.monotonic, repr=False, compare=False)

    @classmethod
    def after(
        cls,
        seconds: float,
        *,
        clock: Clock = time.monotonic,
    ) -> OutboundHttpDeadline:
        """Create a deadline ``seconds`` from the supplied monotonic clock."""

        _validate_timeout(seconds, name="Outbound HTTP deadline")
        now = clock()
        if not math.isfinite(now) or not math.isfinite(now + seconds):
            raise ValueError("Outbound HTTP deadline clock must be finite.")
        return cls(expires_at=now + seconds, clock=clock)

    def remaining(self) -> float:
        """Return remaining seconds, clamped to zero after expiry."""

        remaining = self.expires_at - self.clock()
        return remaining if math.isfinite(remaining) and remaining > 0 else 0.0

    def timeout(self) -> float:
        """Return a positive per-operation timeout or raise after expiry."""

        remaining = self.remaining()
        if remaining <= 0:
            raise OutboundHttpDeadlineExceeded(_GENERIC_TRANSPORT_DETAIL)
        return remaining

    def check(self) -> None:
        """Raise once the request-wide budget has expired."""

        self.timeout()


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
        if "%" in value:
            raise ValueError("Scoped IP addresses are not allowed.")
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


@dataclass(frozen=True, slots=True)
class RegisteredHttpPrefix:
    """One canonical HTTP directory prefix for a registered artifact store.

    ``origin`` is the normalized scheme/host/effective-port tuple.
    ``path_components`` are decoded exactly once and compared structurally.
    ``canonical_url`` is directory-form and always ends in exactly one slash.
    """

    origin: str
    path_components: tuple[str, ...]

    def __post_init__(self) -> None:
        if _canonical_registered_http_prefix_url(
            self.origin,
            self.path_components,
        ) is None:
            raise ValueError("Registered HTTP prefix is invalid.")

    @property
    def canonical_url(self) -> str:
        """Return the derived canonical directory-form URL."""

        canonical_url = _canonical_registered_http_prefix_url(
            self.origin,
            self.path_components,
        )
        if canonical_url is None:  # pragma: no cover - guarded by frozen construction
            raise RuntimeError("Registered HTTP prefix invariant was violated.")
        return canonical_url

    @classmethod
    def parse(cls, value: str) -> RegisteredHttpPrefix | None:
        """Parse a registered base URL without DNS or network access."""

        parsed = _parse_registered_http_absolute_url(value)
        if parsed is None:
            return None
        try:
            return cls(
                origin=parsed.origin,
                path_components=parsed.path_components,
            )
        except ValueError:
            return None

    def compose(self, locator: PortableStorePath) -> str | None:
        """Compose a portable locator beneath this prefix exactly once."""

        if not isinstance(locator, PortableStorePath):
            return None
        combined_components = self.path_components + locator.components
        combined_path = _canonical_registered_http_path(
            combined_components,
            trailing_slash=False,
        )
        if combined_path is None:
            return None
        candidate = f"{self.origin}{combined_path}"
        if len(candidate) > _MAX_URL_LENGTH or not self.contains(candidate):
            return None
        return candidate

    def contains(self, url: str) -> bool:
        """Return whether an absolute URL is structurally inside this prefix."""

        parsed = _parse_registered_http_absolute_url(url)
        if parsed is None or parsed.origin != self.origin:
            return False
        prefix_length = len(self.path_components)
        return parsed.path_components[:prefix_length] == self.path_components

    def resolve_redirect(self, current_url: str, location: str) -> str | None:
        """Resolve one safe redirect that remains inside this prefix.

        The raw ``Location`` is validated before relative resolution so
        ``urljoin`` never receives dot segments, encoded separators, or other
        path-normalization ambiguities.
        """

        current = _parse_registered_http_absolute_url(current_url)
        if (
            current is None
            or not self.contains(current.canonical_url)
            or not _raw_registered_redirect_is_safe(location)
        ):
            return None

        try:
            raw_location = urlsplit(location)
            joined = (
                location
                if raw_location.scheme
                else urljoin(current.canonical_url, location)
            )
        except (TypeError, UnicodeError, ValueError):
            return None
        candidate = _parse_registered_http_absolute_url(joined)
        if candidate is None or not self.contains(candidate.canonical_url):
            return None
        return candidate.canonical_url


class AddressResolver(Protocol):
    """Resolve one logical hostname without making a connection."""

    def resolve(
        self,
        hostname: str,
        port: int,
        *,
        deadline: OutboundHttpDeadline | None = None,
    ) -> Sequence[ApprovedSocketAddress]: ...


AsyncAddressLookup = Callable[[str, float], Awaitable[Sequence[str]]]


async def _system_address_lookup(
    hostname: str,
    lifetime: float,
) -> Sequence[str]:
    resolver = dns.asyncresolver.Resolver(configure=True)
    answers = await resolver.resolve_name(
        hostname,
        family=socket.AF_UNSPEC,
        lifetime=lifetime,
        search=True,
    )
    return tuple(answers.addresses())


class SystemAddressResolver:
    """Resolve A/AAAA records with cancellable asynchronous DNS I/O."""

    def __init__(
        self,
        *,
        lookup: AsyncAddressLookup | None = None,
        default_timeout: float = DEFAULT_OUTBOUND_HTTP_DEADLINE_SECONDS,
    ) -> None:
        _validate_timeout(default_timeout, name="DNS timeout")
        self._lookup = lookup or _system_address_lookup
        self._default_timeout = default_timeout

    def resolve(
        self,
        hostname: str,
        port: int,
        *,
        deadline: OutboundHttpDeadline | None = None,
    ) -> Sequence[ApprovedSocketAddress]:
        active_deadline = (
            deadline
            if deadline is not None
            else OutboundHttpDeadline.after(self._default_timeout)
        )
        active_deadline.check()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            # The public resolver is synchronous and FastAPI runs it in a worker
            # thread. Failing closed here avoids nesting or abandoning event loops.
            raise OSError(_GENERIC_POLICY_DETAIL)

        async def resolve_addresses() -> Sequence[str]:
            timeout = active_deadline.timeout()
            return await asyncio.wait_for(
                self._lookup(hostname, timeout),
                timeout=timeout,
            )

        try:
            raw_addresses = asyncio.run(resolve_addresses())
            active_deadline.check()
            return tuple(
                ApprovedSocketAddress.from_ip(address, port)
                for address in raw_addresses
            )
        except (
            asyncio.TimeoutError,
            dns.exception.DNSException,
            OSError,
            OutboundHttpTransportError,
            ValueError,
        ):
            raise OSError(_GENERIC_POLICY_DETAIL) from None


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
        default_timeout: float = DEFAULT_OUTBOUND_HTTP_DEADLINE_SECONDS,
    ) -> None:
        _validate_timeout(default_timeout, name="Outbound HTTP policy timeout")
        if bool(allowed_authorities) != bool(allowed_networks):
            raise ValueError(
                "Internal HTTP exceptions require both exact authorities and networks."
            )
        self._address_resolver = address_resolver or SystemAddressResolver()
        self._default_timeout = default_timeout
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

    def authorize(
        self,
        url: str,
        *,
        deadline: OutboundHttpDeadline | None = None,
    ) -> ApprovedHttpTarget:
        """Parse, resolve, and authorize one URL without sending request bytes."""

        active_deadline = (
            deadline
            if deadline is not None
            else OutboundHttpDeadline.after(self._default_timeout)
        )
        try:
            active_deadline.check()
        except OutboundHttpTransportError:
            raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL) from None
        parsed = _parse_http_url(url)
        is_internal_exception = parsed.origin in self._allowed_origins
        if _is_local_hostname(parsed.hostname) and not is_internal_exception:
            raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL)

        literal = _ip_literal(parsed.hostname)
        if literal is None:
            try:
                raw_addresses = self._address_resolver.resolve(
                    parsed.hostname,
                    parsed.port,
                    deadline=active_deadline,
                )
            except (OSError, OutboundHttpTransportError):
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

        try:
            active_deadline.check()
        except OutboundHttpTransportError:
            raise OutboundHttpPolicyError(_GENERIC_POLICY_DETAIL) from None
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
        self,
        addresses: Sequence[ApprovedSocketAddress],
        deadline: OutboundHttpDeadline | float,
    ) -> socket.socket: ...


class SystemPinnedSocketConnector:
    """Connect directly to vetted numeric sockaddrs under one shared deadline."""

    def connect(
        self,
        addresses: Sequence[ApprovedSocketAddress],
        deadline: OutboundHttpDeadline | float,
    ) -> socket.socket:
        active_deadline = _coerce_deadline(deadline)
        for address in addresses:
            candidate: socket.socket | None = None
            try:
                active_deadline.check()
                candidate = socket.socket(address.family, address.socktype, address.protocol)
                _set_socket_deadline(candidate, active_deadline)
                candidate.connect(address.sockaddr)
                active_deadline.check()
                peer = candidate.getpeername()
                peer_ip = ipaddress.ip_address(str(peer[0]).split("%", 1)[0])
                peer_port = int(peer[1])
                if peer_ip != address.ip or peer_port != address.sockaddr[1]:
                    _close_quietly(candidate)
                    continue
                _set_socket_deadline(candidate, active_deadline)
                return candidate
            except OutboundHttpDeadlineExceeded:
                if candidate is not None:
                    _close_quietly(candidate)
                raise
            except (OSError, ValueError, OutboundHttpTransportError):
                if candidate is not None:
                    _close_quietly(candidate)
            except BaseException:
                if candidate is not None:
                    _close_preserving_active_exception(candidate)
                raise
        raise OutboundHttpTransportError(_GENERIC_TRANSPORT_DETAIL)


def _validate_timeout(value: float, *, name: str) -> None:
    if (
        not math.isfinite(value)
        or value <= 0
        or value > MAX_OUTBOUND_HTTP_DEADLINE_SECONDS
    ):
        raise ValueError(
            f"{name} must be finite, positive, and no greater than "
            f"{MAX_OUTBOUND_HTTP_DEADLINE_SECONDS:g} seconds."
        )


def _coerce_deadline(
    value: OutboundHttpDeadline | float,
) -> OutboundHttpDeadline:
    if isinstance(value, OutboundHttpDeadline):
        return value
    return OutboundHttpDeadline.after(value)


def _set_socket_deadline(
    raw_socket: socket.socket,
    deadline: OutboundHttpDeadline,
) -> None:
    timeout = min(deadline.timeout(), MAX_OUTBOUND_HTTP_DEADLINE_SECONDS)
    try:
        raw_socket.settimeout(timeout)
    except (OSError, OverflowError, ValueError):
        raise OutboundHttpTransportError(_GENERIC_TRANSPORT_DETAIL) from None


class _DeadlineSocketIO(io.RawIOBase):
    """Raw response stream that reclamps every receive to one total deadline."""

    def __init__(
        self,
        raw_socket: socket.socket,
        deadline: OutboundHttpDeadline,
    ) -> None:
        super().__init__()
        self._socket = raw_socket
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(  # type: ignore[override]
        self,
        buffer: bytearray | memoryview,
    ) -> int | None:
        if self.closed:
            raise ValueError("I/O operation on closed response stream.")
        _set_socket_deadline(self._socket, self._deadline)
        count = self._socket.recv_into(buffer)
        self._deadline.check()
        return count

    def fileno(self) -> int:
        return self._socket.fileno()


class _DeadlineSocket:
    """Minimal socket facade used by ``http.client`` after connection setup."""

    def __init__(
        self,
        raw_socket: socket.socket,
        deadline: OutboundHttpDeadline,
    ) -> None:
        self._socket = raw_socket
        self._deadline = deadline

    def sendall(self, data: bytes, flags: int = 0) -> None:
        _set_socket_deadline(self._socket, self._deadline)
        self._socket.sendall(data, flags)
        self._deadline.check()

    def makefile(
        self,
        mode: str,
        buffering: int | None = None,
    ) -> io.BufferedReader | _DeadlineSocketIO:
        if mode != "rb":
            raise ValueError("Outbound HTTP sockets support binary reads only.")
        raw_stream = _DeadlineSocketIO(self._socket, self._deadline)
        if buffering == 0:
            return raw_stream
        buffer_size = (
            io.DEFAULT_BUFFER_SIZE
            if buffering is None or buffering < 0
            else buffering
        )
        return io.BufferedReader(raw_stream, buffer_size=buffer_size)

    def settimeout(self, _value: float | None) -> None:
        _set_socket_deadline(self._socket, self._deadline)

    def close(self) -> None:
        self._socket.close()


class _Closable(Protocol):
    def close(self) -> None: ...


def _close_quietly(resource: _Closable) -> None:
    # Cleanup must not replace the original generic transport failure.
    with suppress(Exception):
        resource.close()


def _close_preserving_active_exception(resource: _Closable) -> None:
    """Attempt cleanup without replacing an exception already in flight."""

    with suppress(BaseException):
        resource.close()


def _close_resources(*resources: _Closable) -> None:
    """Close all resources, redacting ordinary failures and preserving interrupts."""

    ordinary_failure = False
    base_failure: BaseException | None = None
    for resource in resources:
        try:
            resource.close()
        except Exception:
            ordinary_failure = True
        except BaseException as exc:
            if base_failure is None:
                base_failure = exc
    if base_failure is not None:
        raise base_failure
    if ordinary_failure:
        raise OutboundHttpTransportError(_GENERIC_TRANSPORT_DETAIL) from None


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

    def open(
        self,
        method: str,
        target: ApprovedHttpTarget,
        *,
        deadline: OutboundHttpDeadline | None = None,
    ) -> OutboundHttpResponse: ...


class _StreamingResponse:
    def __init__(
        self,
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
        deadline: OutboundHttpDeadline,
    ) -> None:
        self._connection = connection
        self._response = response
        self._deadline = deadline
        self.status_code = response.status

    def get_header(self, name: str) -> str | None:
        self._deadline.check()
        value = self._response.getheader(name)
        self._deadline.check()
        return value

    def iter_bytes(self) -> Iterator[bytes]:
        try:
            while True:
                self._deadline.check()
                chunk = self._response.read(_STREAM_CHUNK_SIZE)
                self._deadline.check()
                if not chunk:
                    return
                yield chunk
                self._deadline.check()
        except (OSError, http.client.HTTPException, OutboundHttpTransportError):
            _close_quietly(self)
            raise OutboundHttpTransportError(_GENERIC_TRANSPORT_DETAIL) from None

    def close(self) -> None:
        _close_resources(self._response, self._connection)

    def __enter__(self) -> _StreamingResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        if exc is None:
            self.close()
            return
        _close_preserving_active_exception(self)


class _PinnedHttpConnection(http.client.HTTPConnection):
    def __init__(
        self,
        target: ApprovedHttpTarget,
        connector: PinnedSocketConnector,
        deadline: OutboundHttpDeadline,
    ) -> None:
        super().__init__(target.hostname, target.port, timeout=deadline.timeout())
        self._approved_addresses = target.addresses
        self._connector = connector
        self._deadline = deadline

    def connect(self) -> None:
        raw_socket = self._connector.connect(self._approved_addresses, self._deadline)
        try:
            self.sock = cast(socket.socket, _DeadlineSocket(raw_socket, self._deadline))
        except BaseException:
            _close_preserving_active_exception(raw_socket)
            raise


class _PinnedHttpsConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        target: ApprovedHttpTarget,
        connector: PinnedSocketConnector,
        deadline: OutboundHttpDeadline,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(
            target.hostname,
            target.port,
            timeout=deadline.timeout(),
            context=context,
        )
        self._approved_addresses = target.addresses
        self._connector = connector
        self._deadline = deadline
        self._pinned_ssl_context = context

    def connect(self) -> None:
        raw_socket = self._connector.connect(self._approved_addresses, self._deadline)
        wrapped_socket: socket.socket | None = None
        try:
            _set_socket_deadline(raw_socket, self._deadline)
            # ``self.host`` is the canonical logical hostname, not the numeric
            # address, preserving both SNI and certificate hostname checks.
            wrapped_socket = self._pinned_ssl_context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
            self._deadline.check()
            _set_socket_deadline(wrapped_socket, self._deadline)
            self.sock = cast(
                socket.socket,
                _DeadlineSocket(wrapped_socket, self._deadline),
            )
        except BaseException:
            if wrapped_socket is not None and wrapped_socket is not raw_socket:
                _close_preserving_active_exception(wrapped_socket)
            _close_preserving_active_exception(raw_socket)
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
        _validate_timeout(timeout, name="HTTP timeout")
        self._timeout = timeout
        self._connector = (
            connector if connector is not None else SystemPinnedSocketConnector()
        )
        self._ssl_context = (
            ssl_context if ssl_context is not None else ssl.create_default_context()
        )

    def open(
        self,
        method: str,
        target: ApprovedHttpTarget,
        *,
        deadline: OutboundHttpDeadline | None = None,
    ) -> OutboundHttpResponse:
        normalized_method = method.upper()
        if normalized_method not in {"GET", "HEAD"}:
            raise ValueError("SafeHttpClient supports only GET and HEAD.")
        active_deadline = (
            deadline
            if deadline is not None
            else OutboundHttpDeadline.after(self._timeout)
        )
        active_deadline.check()
        connection: http.client.HTTPConnection
        if target.scheme == "https":
            connection = _PinnedHttpsConnection(
                target,
                self._connector,
                active_deadline,
                self._ssl_context,
            )
        else:
            connection = _PinnedHttpConnection(
                target,
                self._connector,
                active_deadline,
            )
        response: http.client.HTTPResponse | None = None
        try:
            active_deadline.check()
            connection.request(
                normalized_method,
                target.request_target,
                headers={
                    "Accept-Encoding": "identity",
                    "User-Agent": "lab-tracker/0.1",
                },
            )
            active_deadline.check()
            response = connection.getresponse()
            active_deadline.check()
            content_encoding = response.getheader("content-encoding")
            if (
                normalized_method != "HEAD"
                and content_encoding
                and content_encoding.strip().lower() != "identity"
            ):
                raise OutboundHttpTransportError(_GENERIC_TRANSPORT_DETAIL)
            return _StreamingResponse(connection, response, active_deadline)
        except Exception:
            if response is not None:
                try:
                    _close_quietly(response)
                except BaseException:
                    _close_preserving_active_exception(connection)
                    raise
            _close_quietly(connection)
            raise OutboundHttpTransportError(_GENERIC_TRANSPORT_DETAIL) from None
        except BaseException:
            if response is not None:
                _close_preserving_active_exception(response)
            _close_preserving_active_exception(connection)
            raise


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


@dataclass(frozen=True, slots=True)
class _ParsedRegisteredHttpUrl:
    origin: str
    path_components: tuple[str, ...]
    has_trailing_slash: bool

    @property
    def canonical_url(self) -> str:
        canonical_url = _canonical_registered_http_url(
            self.origin,
            self.path_components,
            trailing_slash=self.has_trailing_slash,
        )
        if canonical_url is None:  # pragma: no cover - private parser establishes this
            raise RuntimeError("Parsed registered HTTP URL invariant was violated.")
        return canonical_url


def _parse_registered_http_absolute_url(
    value: object,
) -> _ParsedRegisteredHttpUrl | None:
    if not _registered_http_text_is_safe(value) or not isinstance(value, str):
        return None
    if "?" in value or "#" in value:
        return None
    try:
        split = urlsplit(value)
        parsed = _parse_http_url(value)
    except (OutboundHttpPolicyError, UnicodeError, ValueError):
        return None
    if split.query or split.fragment:
        return None

    parsed_path = _parse_registered_http_path(split.path, absolute=True)
    if parsed_path is None:
        return None
    path_components, has_trailing_slash = parsed_path
    canonical_url = _canonical_registered_http_url(
        parsed.origin,
        path_components,
        trailing_slash=has_trailing_slash,
    )
    if canonical_url is None:
        return None
    return _ParsedRegisteredHttpUrl(
        origin=parsed.origin,
        path_components=path_components,
        has_trailing_slash=has_trailing_slash,
    )


def _canonical_registered_http_prefix_url(
    origin: object,
    path_components: object,
) -> str | None:
    if not isinstance(origin, str) or not isinstance(path_components, tuple):
        return None
    try:
        parsed_origin = _parse_http_url(origin)
    except OutboundHttpPolicyError:
        return None
    if parsed_origin.origin != origin or parsed_origin.absolute_url != f"{origin}/":
        return None
    return _canonical_registered_http_url(
        origin,
        path_components,
        trailing_slash=True,
    )


def _canonical_registered_http_url(
    origin: str,
    path_components: tuple[str, ...],
    *,
    trailing_slash: bool,
) -> str | None:
    canonical_path = _canonical_registered_http_path(
        path_components,
        trailing_slash=trailing_slash,
    )
    if canonical_path is None:
        return None
    canonical_url = f"{origin}{canonical_path}"
    return canonical_url if len(canonical_url) <= _MAX_URL_LENGTH else None


def _canonical_registered_http_path(
    components: tuple[str, ...],
    *,
    trailing_slash: bool,
) -> str | None:
    if not isinstance(components, tuple):
        return None
    if not components:
        return "/"
    try:
        portable_path = PortableStorePath(components)
    except (TypeError, ValueError):
        return None
    if any(
        ";" in unicodedata.normalize("NFKC", component)
        for component in portable_path.components
    ):
        return None
    suffix = "/" if trailing_slash else ""
    return f"/{portable_path.uri_path}{suffix}"


def _parse_registered_http_path(
    raw_path: str,
    *,
    absolute: bool,
) -> tuple[tuple[str, ...], bool] | None:
    if not isinstance(raw_path, str) or (
        raw_path and not _registered_http_text_is_safe(raw_path)
    ):
        return None
    if "//" in raw_path:
        return None
    if absolute:
        if raw_path and not raw_path.startswith("/"):
            return None
        path = raw_path[1:] if raw_path.startswith("/") else ""
    else:
        path = raw_path[1:] if raw_path.startswith("/") else raw_path

    has_trailing_slash = not path or path.endswith("/")
    if path.endswith("/"):
        path = path[:-1]
    if not path:
        return (), has_trailing_slash

    portable_path = PortableStorePath.parse_uri_path(path)
    if portable_path is None:
        return None
    canonical_path = _canonical_registered_http_path(
        portable_path.components,
        trailing_slash=False,
    )
    if canonical_path != f"/{path}":
        return None
    return portable_path.components, has_trailing_slash


def _raw_registered_redirect_is_safe(location: object) -> bool:
    if (
        not _registered_http_text_is_safe(location)
        or not isinstance(location, str)
        or not location
        or "?" in location
        or "#" in location
        or location.startswith("//")
    ):
        return False
    try:
        split = urlsplit(location)
    except (UnicodeError, ValueError):
        return False
    if split.query or split.fragment:
        return False
    if split.scheme:
        return _parse_registered_http_absolute_url(location) is not None
    if split.netloc:
        return False
    return _parse_registered_http_path(split.path, absolute=False) is not None


def _registered_http_text_is_safe(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= _MAX_URL_LENGTH
        and "\\" not in value
        and ";" not in value
        and not any(unicodedata.category(character) == "Cc" for character in value)
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
